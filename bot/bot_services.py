# bot_services.py
import os

# Совместимость со старым bot_main.py, где имя переменной прокси
# ошибочно читается с пробелом в конце: "TELEGRAM_PROXY ".
# inf.env при этом должен содержать нормальный ключ TELEGRAM_PROXY.
_telegram_proxy = (os.getenv("TELEGRAM_PROXY") or "").strip()
if _telegram_proxy:
    os.environ["TELEGRAM_PROXY "] = _telegram_proxy

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot_common import send_or_edit, show_main_menu, STATE_IDLE
from bot_client_chat import CC_WAIT_CONTENT, create_client_chat_text_request, _kb_content_prompt

from db import get_nav_snapshot

# profession ID для «Услуги» (заявки директору/отделу услуг)
# В БД у специалистов/директора эта профессия должна быть = 2
PROF_SERVICES = 2
PROF_PERMIT_DOCS = 4

GIS_SYSTEMS = {
    "ts": "ТС Пиот",
    "chz": "Честный знак",
    "egais": "ЕГАИС",
    "edo": "ЭДО",
}

GIS_ACTIONS = {
    "reg": "Регистрация",
    "setup": "Настройка",
    "audit": "Аудит / проверка работы",
    "consult": "Консультация",
}

REG_ITEMS = {
    "fn": "Замена ФН",
    "ofd": "Продление ОФД",
    "kkt": "Регистрация ККТ",
    "upd": "Обновления",
}

NEWBIZ_ITEMS = {
    "best": "Подбор выгодного решения",
    "equip": "Подбор оборудования",
    # «Открытие ИП» вынесено в корень раздела «Услуги» — отдельная логика
    # с прямой подачей заявки в Альфа-Банк (см. bot/bot_alfabank_ip.py).
    "kassa10000": "🎁 Акции",
}

PROMPT_TEXT = (
    "Здравствуйте!\n\n"
    "По возможности максимально подробно изложите суть вопроса, с которым вы столкнулись.\n\n"
    "Пример: касса Эвотор 7, показывает ошибку №24, не даёт принять накладные."
)



async def _require_verified_for_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Подача заявок в разделе «Услуги» доступна только верифицированным пользователям."""
    try:
        uid = int(update.effective_user.id)
    except Exception:
        return False

    snap = await get_nav_snapshot(uid, tg_username=context.user_data.get("tg_username"))
    if bool(snap.get("verified")):
        return True

    await send_or_edit(
        update,
        "⛔ Подача заявки доступна только после верификации профиля.\n\n"
        "Подтвердите телефон и укажите ИНН.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
            [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ]),
        parse_mode=None,
    )
    return False

def _set_cc_ctx(context: ContextTypes.DEFAULT_TYPE, *, request_type: str, category: str):
    context.user_data["client_chat_ctx"] = {
        "profession": int(PROF_SERVICES),
        "request_type": request_type,
        "category": category,
        "promo_code": None,
    }
    # на всякий — сбросить черновики/режимы ответа
    context.user_data.pop("cc_draft", None)
    context.user_data.pop("cc_reply_chat_id", None)


def _set_permit_docs_ctx(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["client_chat_ctx"] = {
        "profession": int(PROF_PERMIT_DOCS),
        "request_type": "Сертификаты / Доп. декларации",
        "category": "Сертификаты / Доп. декларации",
        "promo_code": None,
    }
    context.user_data.pop("cc_draft", None)
    context.user_data.pop("cc_reply_chat_id", None)


async def svc_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_services(update, context):
        return STATE_IDLE
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏢 Новый Бизнес / Открыть ИП/ООО", callback_data="svc_ip")],
        [InlineKeyboardButton("🎁 Акции / Выгодные решения", callback_data="svc_newbiz")],
        [InlineKeyboardButton("🏛 ТС ПИоТ / Честный Знак", callback_data="svc_gis")],
        [InlineKeyboardButton("📝 Подписать: накладные/УПД/ЕГАИС", callback_data="svc_leaf:upd_sign")],
        [InlineKeyboardButton("🖨 Регистрация Кассы", callback_data="svc_leaf:reg:kkt")],
        [InlineKeyboardButton("🔄 Замена ФН", callback_data="svc_leaf:reg:fn")],
        [InlineKeyboardButton("🔑 Выпуск ЭЦП / МЧД", callback_data="svc_leaf:ecp")],
        [InlineKeyboardButton("🧻 Кассовая Лента / доставка", callback_data="shop_paper")],
        [InlineKeyboardButton("💼 1С / Сбис / Контур", callback_data="svc_leaf:1c_soft")],
        [InlineKeyboardButton("🍺 ЕГАИС / ЭТО", callback_data="svc_leaf:egais_eto")],
        [InlineKeyboardButton("📄 Сертификаты / Декларации", callback_data="svc_leaf:cert")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await send_or_edit(update, "🧰 Услуги — выберите раздел:", kb, parse_mode=None)
    return CC_WAIT_CONTENT


async def svc_gis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(GIS_SYSTEMS["ts"], callback_data="svc_gis_sys:ts")],
        [InlineKeyboardButton(GIS_SYSTEMS["chz"], callback_data="svc_gis_sys:chz")],
        [InlineKeyboardButton(GIS_SYSTEMS["egais"], callback_data="svc_gis_sys:egais")],
        [InlineKeyboardButton(GIS_SYSTEMS["edo"], callback_data="svc_gis_sys:edo")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="svc_root")],
    ])
    await send_or_edit(update, "Выберите систему:", kb, parse_mode=None)
    return CC_WAIT_CONTENT


async def svc_gis_sys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    sys_key = (q.data or "").split(":", 1)[1]
    sys_name = GIS_SYSTEMS.get(sys_key, sys_key)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(GIS_ACTIONS["reg"], callback_data=f"svc_leaf:gis:{sys_key}:reg")],
        [InlineKeyboardButton(GIS_ACTIONS["setup"], callback_data=f"svc_leaf:gis:{sys_key}:setup")],
        [InlineKeyboardButton(GIS_ACTIONS["audit"], callback_data=f"svc_leaf:gis:{sys_key}:audit")],
        [InlineKeyboardButton(GIS_ACTIONS["consult"], callback_data=f"svc_leaf:gis:{sys_key}:consult")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="svc_gis")],
    ])
    await send_or_edit(update, f"{sys_name}: выберите действие:", kb, parse_mode=None)
    return CC_WAIT_CONTENT


async def svc_reg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(REG_ITEMS["fn"], callback_data="svc_leaf:reg:fn")],
        [InlineKeyboardButton(REG_ITEMS["ofd"], callback_data="svc_leaf:reg:ofd")],
        [InlineKeyboardButton(REG_ITEMS["kkt"], callback_data="svc_leaf:reg:kkt")],
        [InlineKeyboardButton(REG_ITEMS["upd"], callback_data="svc_leaf:reg:upd")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="svc_root")],
    ])
    await send_or_edit(update, "Выберите услугу:", kb, parse_mode=None)
    return CC_WAIT_CONTENT


async def svc_newbiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(NEWBIZ_ITEMS["best"], callback_data="svc_leaf:newbiz:best")],
        [InlineKeyboardButton(NEWBIZ_ITEMS["equip"], callback_data="svc_leaf:newbiz:equip")],
        [InlineKeyboardButton(NEWBIZ_ITEMS["kassa10000"], callback_data="svc_leaf:newbiz:kassa10000")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="svc_root")],
    ])
    await send_or_edit(update, "🎁 Акции и скидки — выберите:", kb, parse_mode=None)
    return CC_WAIT_CONTENT


async def svc_leaf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_services(update, context):
        return STATE_IDLE

    q = update.callback_query
    data = (q.data or "").split(":")

    # варианты:
    # svc_leaf:reg:fn
    # svc_leaf:newbiz:ip
    # svc_leaf:gis:chz:setup
    # svc_leaf:cert
    group = data[1]

    if group == "gis":
        sys_key = data[2]
        act_key = data[3]
        request_type = "Гос. инф. системы"
        category = f"{GIS_SYSTEMS.get(sys_key, sys_key)} — {GIS_ACTIONS.get(act_key, act_key)}"
    elif group == "reg":
        request_type = "Регистрация / Замена / Обновления / Продление"
        category = REG_ITEMS.get(data[2], data[2])
    elif group == "upd_sign":
        _set_cc_ctx(context, request_type="Услуги", category="Подписать накладные // УПД")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Все", callback_data="svc_leaf:upd_sign_confirm")],
            [InlineKeyboardButton("💬 Частично", callback_data="svc_leaf:upd_sign_comments")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
        ])
        await send_or_edit(
            update,
            "Подписать накладные // УПД — выберите действие:",
            kb,
            parse_mode=None,
        )
        return STATE_IDLE
    elif group == "upd_sign_confirm":
        _set_cc_ctx(context, request_type="Услуги", category="Подписать накладные // УПД")
        return await create_client_chat_text_request(
            update,
            context,
            text="Подтвердить подписание",
        )
    elif group == "upd_sign_comments":
        request_type = "Услуги"
        category = "Подписать накладные // УПД"
    elif group == "ecp":
        request_type = "Услуги"
        category = "Выпуск ЭЦП / МЧД"
    elif group == "1c_soft":
        request_type = "Услуги"
        category = "1С / Сбис / Контур"
    elif group == "egais_eto":
        request_type = "Услуги"
        category = "ЕГАИС / ЭТО"
    elif group == "cert":
        _set_permit_docs_ctx(context)
        await send_or_edit(update, PROMPT_TEXT, _kb_content_prompt(PROF_PERMIT_DOCS), parse_mode=None)
        return CC_WAIT_CONTENT
    else:
        request_type = "Акции и скидки"
        category = NEWBIZ_ITEMS.get(data[2], data[2])

    _set_cc_ctx(context, request_type=request_type, category=category)

    await send_or_edit(update, PROMPT_TEXT, _kb_content_prompt(PROF_SERVICES), parse_mode=None)
    return CC_WAIT_CONTENT
