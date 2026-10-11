"""
max_services.py — Меню услуг для MAX-бота.

Перенос логики из bot_services.py:
- Трёхуровневая навигация: раздел → система/услуга → действие → промпт
- В финале устанавливается контекст для клиентского чата (client_chat_ctx)
- Пока клиентский чат не перенесён — финальный шаг показывает промпт + возврат в меню
"""

import logging

from max_api import MaxCallback, InlineKeyboard, InlineButton
from max_bot_main import MaxBotContext
from max_registration import _send_or_edit, _set_state, ST_IDLE, _resolve_chat_id
from max_client_chat import ST_CC_WAIT_CONTENT, create_client_chat_text_request_max
from db import is_user_verified_by_internal_id

logger = logging.getLogger(__name__)

# profession ID для «Услуги» — в БД specialists.profession = 2
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
    "ip": "Открытие ИП",
    "kassa10000": "🎁 Акции",
}

PROMPT_TEXT = (
    "Здравствуйте!\n\n"
    "По возможности максимально подробно изложите суть вопроса, с которым вы столкнулись.\n\n"
    "Пример: касса Эвотор 7, показывает ошибку №24, не даёт принять накладные."
)


def _set_cc_ctx(ctx: MaxBotContext, *, request_type: str, category: str) -> None:
    """Установить контекст для будущего клиентского чата."""
    ctx.user_data["client_chat_ctx"] = {
        "profession": int(PROF_SERVICES),
        "request_type": request_type,
        "category": category,
        "promo_code": None,
    }
    ctx.user_data.pop("cc_draft", None)
    ctx.user_data.pop("cc_reply_chat_id", None)


def _set_permit_docs_ctx(ctx: MaxBotContext) -> None:
    """Установить контекст для обращений к специалисту по разрешительной документации."""
    ctx.user_data["client_chat_ctx"] = {
        "profession": int(PROF_PERMIT_DOCS),
        "request_type": "Сертификаты / Доп. декларации",
        "category": "Сертификаты / Доп. декларации",
        "promo_code": None,
    }
    ctx.user_data.pop("cc_draft", None)
    ctx.user_data.pop("cc_reply_chat_id", None)


async def _require_verified_for_services(ctx: MaxBotContext, chat_id: str, *, message_id: str | None = None) -> bool:
    """Раздел услуг и создание обращений по услугам доступны только верифицированным."""
    db_id = int(ctx.user_data.get("_db_id") or 0)
    verified = bool(ctx.user_data.get("_verified"))

    if db_id and not verified:
        try:
            verified = bool(await is_user_verified_by_internal_id(db_id))
        except Exception:
            verified = False
        ctx.user_data["_verified"] = verified

    if verified:
        return True

    kb = InlineKeyboard(rows=[
        [InlineButton("🔐 Закончить верификацию", callback_data="register")],
        [InlineButton("👤 Мой профиль", callback_data="profile")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(
        ctx,
        chat_id,
        "⛔ Услуги и подача обращений доступны только после верификации.\n\n"
        "Подтвердите телефон и укажите ИНН.",
        keyboard=kb,
        message_id=message_id,
    )
    return False


# ── Корень меню услуг ─────────────────────────────────────────────────

async def handle_svc_root(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Главное меню услуг — 3 раздела."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    if not await _require_verified_for_services(ctx, chat_id, message_id=mid):
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("🏢 Новый Бизнес / Открыть ИП/ООО", callback_data="svc_ip")],
        [InlineButton("🎁 Акции / Выгодные решения", callback_data="svc_newbiz")],
        [InlineButton("🏛 ТС ПИоТ / Честный Знак", callback_data="svc_gis")],
        [InlineButton("📝 Подписать: накладные/УПД/ЕГАИС", callback_data="svc_leaf:upd_sign")],
        [InlineButton("🖨 Регистрация Кассы", callback_data="svc_leaf:reg:kkt")],
        [InlineButton("🔄 Замена ФН", callback_data="svc_leaf:reg:fn")],
        [InlineButton("🔑 Выпуск ЭЦП / МЧД", callback_data="svc_leaf:ecp")],
        [InlineButton("🧻 Кассовая Лента / доставка", callback_data="shop_paper")],
        [InlineButton("💼 1С / Сбис / Контур", callback_data="svc_leaf:1c_soft")],
        [InlineButton("🍺 ЕГАИС / ЭТО", callback_data="svc_leaf:egais_eto")],
        [InlineButton("📄 Сертификаты / Декларации", callback_data="svc_leaf:cert")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, "🧰 Услуги — выберите раздел:", keyboard=kb, message_id=mid)


# ── ГИС — выбор системы ──────────────────────────────────────────────

async def handle_svc_gis(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Подменю: выбор гос. информационной системы."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    kb = InlineKeyboard(rows=[
        [InlineButton(GIS_SYSTEMS["ts"], callback_data="svc_gis_sys:ts")],
        [InlineButton(GIS_SYSTEMS["chz"], callback_data="svc_gis_sys:chz")],
        [InlineButton(GIS_SYSTEMS["egais"], callback_data="svc_gis_sys:egais")],
        [InlineButton(GIS_SYSTEMS["edo"], callback_data="svc_gis_sys:edo")],
        [InlineButton("⬅️ Назад", callback_data="svc_root")],
    ])
    await _send_or_edit(ctx, chat_id, "Выберите систему:", keyboard=kb, message_id=mid)


# ── ГИС — выбор действия ─────────────────────────────────────────────

async def handle_svc_gis_sys(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Подменю: выбор действия для конкретной ГИС."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    payload = cb.payload or ""
    sys_key = payload.split(":", 1)[1] if ":" in payload else ""
    sys_name = GIS_SYSTEMS.get(sys_key, sys_key)

    kb = InlineKeyboard(rows=[
        [InlineButton(GIS_ACTIONS["reg"], callback_data=f"svc_leaf:gis:{sys_key}:reg")],
        [InlineButton(GIS_ACTIONS["setup"], callback_data=f"svc_leaf:gis:{sys_key}:setup")],
        [InlineButton(GIS_ACTIONS["audit"], callback_data=f"svc_leaf:gis:{sys_key}:audit")],
        [InlineButton(GIS_ACTIONS["consult"], callback_data=f"svc_leaf:gis:{sys_key}:consult")],
        [InlineButton("⬅️ Назад", callback_data="svc_gis")],
    ])
    await _send_or_edit(ctx, chat_id, f"{sys_name}: выберите действие:", keyboard=kb, message_id=mid)


# ── Регистрация / Замена / Обновления ─────────────────────────────────

async def handle_svc_reg(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Подменю: регистрация/замена/обновления/продление."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    kb = InlineKeyboard(rows=[
        [InlineButton(REG_ITEMS["fn"], callback_data="svc_leaf:reg:fn")],
        [InlineButton(REG_ITEMS["ofd"], callback_data="svc_leaf:reg:ofd")],
        [InlineButton(REG_ITEMS["kkt"], callback_data="svc_leaf:reg:kkt")],
        [InlineButton(REG_ITEMS["upd"], callback_data="svc_leaf:reg:upd")],
        [InlineButton("⬅️ Назад", callback_data="svc_root")],
    ])
    await _send_or_edit(ctx, chat_id, "Выберите услугу:", keyboard=kb, message_id=mid)


# ── Для нового бизнеса ───────────────────────────────────────────────

async def handle_svc_newbiz(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Подменю: услуги для нового бизнеса."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    kb = InlineKeyboard(rows=[
        [InlineButton(NEWBIZ_ITEMS["best"], callback_data="svc_leaf:newbiz:best")],
        [InlineButton(NEWBIZ_ITEMS["equip"], callback_data="svc_leaf:newbiz:equip")],
        [InlineButton(NEWBIZ_ITEMS["ip"], callback_data="svc_leaf:newbiz:ip")],
        [InlineButton(NEWBIZ_ITEMS["kassa10000"], callback_data="svc_leaf:newbiz:kassa10000")],
        [InlineButton("⬅️ Назад", callback_data="svc_root")],
    ])
    await _send_or_edit(ctx, chat_id, "🎁 Акции и скидки — выберите:", keyboard=kb, message_id=mid)


# ── Финальный шаг — промпт для описания проблемы ─────────────────────

async def handle_svc_leaf(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """
    Финальный экран: показываем промпт для описания проблемы.
    Устанавливаем client_chat_ctx для дальнейшей передачи в чат со специалистом.

    Формат payload:
    - svc_leaf:gis:<sys_key>:<act_key>
    - svc_leaf:reg:<item_key>
    - svc_leaf:cert
    - svc_leaf:newbiz:<item_key>
    """
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    if not await _require_verified_for_services(ctx, chat_id, message_id=mid):
        return

    payload = cb.payload or ""
    parts = payload.split(":")
    # parts[0] = "svc_leaf", parts[1] = group, parts[2..] = keys

    group = parts[1] if len(parts) > 1 else ""

    if group == "gis":
        sys_key = parts[2] if len(parts) > 2 else ""
        act_key = parts[3] if len(parts) > 3 else ""
        request_type = "Гос. инф. системы"
        category = f"{GIS_SYSTEMS.get(sys_key, sys_key)} — {GIS_ACTIONS.get(act_key, act_key)}"
    elif group == "reg":
        item_key = parts[2] if len(parts) > 2 else ""
        request_type = "Регистрация / Замена / Обновления / Продление"
        category = REG_ITEMS.get(item_key, item_key)
    elif group == "upd_sign":
        _set_cc_ctx(ctx, request_type="Услуги", category="Подписать накладные // УПД")
        kb = InlineKeyboard(rows=[
            [InlineButton("✅ Все", callback_data="svc_leaf:upd_sign_confirm")],
            [InlineButton("💬 Частично", callback_data="svc_leaf:upd_sign_comments")],
            [InlineButton("🔙 В меню", callback_data="back_to_main")],
        ])
        await _send_or_edit(
            ctx,
            chat_id,
            "Подписать накладные // УПД — выберите действие:",
            keyboard=kb,
            message_id=mid,
        )
        _set_state(ctx, ST_IDLE)
        return
    elif group == "upd_sign_confirm":
        _set_cc_ctx(ctx, request_type="Услуги", category="Подписать накладные // УПД")
        max_user_id = str(cb.user.user_id) if cb.user else ""
        await create_client_chat_text_request_max(
            ctx,
            chat_id=chat_id,
            max_user_id=max_user_id,
            text="Подтвердить подписание",
            message_id=mid,
        )
        return
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
        _set_permit_docs_ctx(ctx)
        kb = InlineKeyboard(rows=[
            [InlineButton("🔙 Назад", callback_data="svc_root")],
            [InlineButton("🔙 В меню", callback_data="back_to_main")],
        ])
        text = f"📋 Сертификаты / Доп. декларации\n\n{PROMPT_TEXT}"
        await _send_or_edit(ctx, chat_id, text, keyboard=kb, message_id=mid)
        _set_state(ctx, ST_CC_WAIT_CONTENT)
        return
    else:
        item_key = parts[2] if len(parts) > 2 else ""
        request_type = "Акции и скидки"
        category = NEWBIZ_ITEMS.get(item_key, item_key)

    _set_cc_ctx(ctx, request_type=request_type, category=category)

    kb = InlineKeyboard(rows=[
        [InlineButton("🔙 Назад", callback_data="svc_root")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    text = (
        f"📋 {request_type} → {category}\n\n"
        f"{PROMPT_TEXT}"
    )
    await _send_or_edit(ctx, chat_id, text, keyboard=kb, message_id=mid)
    _set_state(ctx, ST_CC_WAIT_CONTENT)


# ══════════════════════════════════════════════════════════════════════
# Регистрация обработчиков
# ══════════════════════════════════════════════════════════════════════

def register_services_handlers(bot) -> None:
    """Подключить обработчики меню услуг к MaxBot."""
    # Используем алиас для совместимости с главным меню
    bot.register_callback("services", handle_svc_root)
    bot.register_callback("svc_root", handle_svc_root)

    # ГИС
    bot.register_callback("svc_gis", handle_svc_gis)
    bot.register_callback("svc_gis_sys:*", handle_svc_gis_sys)

    # Регистрация/замена
    bot.register_callback("svc_reg", handle_svc_reg)

    # Новый бизнес
    bot.register_callback("svc_newbiz", handle_svc_newbiz)

    # Финальный экран (все варианты)
    bot.register_callback("svc_leaf:*", handle_svc_leaf)

    logger.info("MAX-бот: обработчики услуг подключены")
