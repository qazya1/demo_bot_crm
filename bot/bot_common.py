# bot_common.py
import os
import time
import logging

import asyncio
from typing import Optional, Tuple, Dict, Any

# --- Tester checkout ---
# Telegram user IDs listed here (or in env TESTER_TG_IDS) проходят checkout по заглушке: заказ
# подтверждается автоматически без Tinkoff, но в БД сохраняется реальная сумма заказа.
TESTER_TG_IDS: set[int] = set()

_env_ids = os.getenv("TESTER_TG_IDS", "").strip()
if _env_ids:
    for _x in _env_ids.split(","):
        _x = _x.strip()
        if _x.isdigit():
            TESTER_TG_IDS.add(int(_x))


def is_tester(tg_user_id) -> bool:
    try:
        return int(tg_user_id) in TESTER_TG_IDS
    except Exception:
        return False


from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from webapp_settings import WEBAPP_URL
from telegram.error import BadRequest
from message_cleanup import (
    send_or_replace_menu_tg,
    cleanup_temporary_messages_tg,
    send_temporary_message_tg,
    safe_delete_message_tg,
)
from ui_flow import TelegramUIFlow, DEFAULT_POLICY

# Единый экземпляр TelegramUIFlow для всего бота
_ui = TelegramUIFlow(DEFAULT_POLICY)

import os
import sys

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
from db import ui_get_main_menu_mid, ui_set_main_menu_mid
from db import (
    get_user,
    get_user_consent,
    is_user_verified,
    get_specialist_by_tg,
    get_specialist_by_max,
    is_partner_specialist,
    get_nav_snapshot,
    count_pending_unassigned,
    count_consultations,
    create_chat_access_token
)
from db import log_event
import hashlib

logger = logging.getLogger(__name__)

# ===== Conversation states (единый источник правды) =====
(
    STATE_IDLE,
    STATE_CONSENT,
    STATE_PRODUCT_SELECT,
    STATE_CART,
    STATE_CO_EMAIL,
    STATE_CO_INN,
    STATE_CO_CITY,
    STATE_ADMIN_ADD_CODES,
    STATE_EDIT_MENU,
    STATE_EDIT_FIRSTNAME,
    STATE_EDIT_LASTNAME,
    STATE_EDIT_PHONE,
    STATE_EDIT_EMAIL,
    STATE_EDIT_INN,
    STATE_EDIT_CITY,

    # Новая FSM идентификации
    S0_START,
    S0_GUEST,
    S1_IDENT,
    S2_WAIT_CONTACT,
    S4_WAIT_INN,
    S5_HOME,

    # Анкета (заявка/оформление)
    STATE_FORM_TP,
    STATE_FORM_ADDR,
    STATE_FORM_ACT,
    STATE_FORM_ASSORT,
    STATE_FORM_SERVICES,
    STATE_FORM_SNO,
    STATE_FORM_MARKED,
    STATE_FORM_FNTERM,
    STATE_FORM_OFD,
    STATE_FORM_BANKS,
    STATE_CO_PHONE,
    STATE_CO_KKT_FACTORY_NUMBER,
    STATE_DIR_SET_SERVICE_CODE,
    STATE_CO_BONUS,
    STATE_CO_ADDRESS,
) = range(36)

# Флаг и версия согласия (можно держать тут)
RU_FLAG = "🇷🇺"
CONSENT_VERSION = 1  # синхронизируешь с БД/миграциями

# Админы
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}




def _is_inline_notice_message_text(text: str | None) -> bool:
    """Сообщения-notice с inline-кнопками должны удаляться при нажатии."""
    t = (text or "").strip().lower()
    if not t:
        return False
    prefixes = (
        "💬 новое сообщение",
        "✅ ваша заявка принята",
        "ваша заявка принята",
        "📨 вам назначено новое обращение",
        "⚠️ все специалисты отказались",
        "✅ оплата подтверждена",
    )
    return any(t.startswith(prefix.lower()) for prefix in prefixes)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_specialist_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    True если текущий TG-пользователь — специалист (есть в таблице specialists и is_active=1).
    Кешируем True в context.user_data. False не кешируем — запись может появиться позже
    (например, после привязки max_user_id).
    """
    try:
        tg_user = update.effective_user
        uid = int(tg_user.id)
        uname = (tg_user.username or "").strip()
        spec = await get_specialist_by_tg(tg_user_id=uid, tg_username=uname)

        if not spec:
            # Fallback: specialist may be linked only via max_user_id
            user_row = await get_user(str(uid))
            max_uid = int((user_row or {}).get("max_user_id") or 0)
            if max_uid:
                spec = await get_specialist_by_max(max_user_id=max_uid)

        is_spec = bool(spec) and not is_partner_specialist(spec)
        if is_spec:
            context.user_data["_is_specialist"] = True
        else:
            context.user_data.pop("_is_specialist", None)
            context.user_data.pop("agent_ctx", None)
        return is_spec
    except Exception:
        context.user_data.pop("_is_specialist", None)
        return False


def _digits(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


async def _has_consent(uid: str) -> bool:
    try:
        c = await get_user_consent(uid) or {}
        is_user_has_consent = bool(c.get("consent_accepted_at")) and int(c.get("consent_version", 0)) >= CONSENT_VERSION
        if not is_user_has_consent:
            spec = await get_specialist_by_tg(tg_user_id=int(uid), tg_username=None)
            if not spec:
                # Fallback: specialist may be linked only via max_user_id
                user_row = await get_user(str(uid))
                max_uid = int((user_row or {}).get("max_user_id") or 0)
                if max_uid:
                    spec = await get_specialist_by_max(max_user_id=max_uid)
            if not spec:
                return False
        return True
    except Exception as e:
        return False


REPLY_TTL_SEC = 10 * 60  # 10 минут


def _reply_store(context) -> Dict[int, Dict[str, Any]]:
    """
    Глобальное хранилище reply-флагов.
    Ключ: user_id получателя
    Значение: {"mode": "cc"/"ag", "chat_id": int, "until": int}
    """
    store = context.application.bot_data.setdefault("reply_arms", {})
    # store: dict[user_id] -> payload
    return store


def clear_chat_reply_flags(context, user_id: int) -> None:
    """Снять флаг у пользователя (при любом действии)."""
    store = _reply_store(context)
    store.pop(int(user_id), None)


def cleanup_expired_reply_flags(context, user_id: int) -> None:
    """Если TTL истёк — чистим."""
    store = _reply_store(context)
    uid = int(user_id)
    payload = store.get(uid)
    if not payload:
        return
    until = int(payload.get("until") or 0)
    if until and until <= int(time.time()):
        store.pop(uid, None)


def arm_chat_reply(context, *, user_id: int, mode: str, chat_id: int, ttl_sec: int = REPLY_TTL_SEC) -> None:
    """
    Вооружаем пользователя:
    mode = "cc" -> следующий текст пользователя будет считаться ответом в клиентском чате
    mode = "ag" -> следующий текст пользователя будет считаться ответом в агентском чате
    """
    if mode not in ("cc", "ag"):
        raise ValueError(f"Unknown mode: {mode}")

    store = _reply_store(context)
    store[int(user_id)] = {
        "mode": mode,
        "chat_id": int(chat_id),
        "until": int(time.time()) + int(ttl_sec),
    }


def get_armed_reply(context, user_id: int) -> Optional[Tuple[str, int]]:
    """Вернёт ("cc"/"ag", chat_id), если есть активный флаг."""
    cleanup_expired_reply_flags(context, int(user_id))
    store = _reply_store(context)
    payload = store.get(int(user_id))
    if not payload:
        return None
    return str(payload["mode"]), int(payload["chat_id"])


def kb_request_contact() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="📲 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _debounce_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, window: float = 0.4) -> bool:
    """
    True -> обработку можно продолжать
    False -> пропустить (слишком частый повтор того же колбэка)
    """
    q = update.callback_query
    if not q:
        return True
    now = time.time()
    last_ts = context.user_data.get("_last_cb_ts", 0.0)
    last_data = context.user_data.get("_last_cb_data")
    allow = not (q.data == last_data and (now - last_ts) < window)
    context.user_data["_last_cb_ts"] = now
    context.user_data["_last_cb_data"] = q.data
    return allow


async def send_or_edit(
        update: Update,
        text: str,
        reply_markup: InlineKeyboardMarkup,
        parse_mode=None,
        disable_web_page_preview=True,
        context: ContextTypes.DEFAULT_TYPE | None = None,
):
    """
    Универсалка: если пришёл callback — пробуем редактировать сообщение,
    иначе шлём новое.
    """
    from telegram.error import BadRequest  # локальный импорт, чтобы не плодить зависимостей
    def _kb_digest(reply_markup: InlineKeyboardMarkup) -> list[dict]:
        if not reply_markup:
            return []
        out = []
        try:
            for row in reply_markup.inline_keyboard or []:
                for b in row:
                    out.append({
                        "t": b.text,
                        "d": b.callback_data,
                        "u": b.url,
                        "w": bool(getattr(b, "web_app", None)),
                    })
        except Exception:
            pass
        return out[:40]  # ограничим размер

    # внутри send_or_edit(), после получения update:
    try:
        uid = int(update.effective_user.id) if update.effective_user else None
        cid = int(update.effective_chat.id) if update.effective_chat else None
        title = (text.splitlines()[0] if text else "")[:120]
        h = hashlib.sha1((text or "").encode("utf-8")).hexdigest()

        await log_event(
            event_type="screen",
            tg_user_id=uid,
            chat_id=cid,
            update_id=getattr(update, "update_id", None),
            name=title or "screen",
            payload={
                "hash": h,
                "title": title,
                "buttons": _kb_digest(reply_markup),
            },
        )
    except Exception:
        pass
    if update.callback_query:
        try:
            await update.callback_query.answer(cache_time=0)
        except Exception as e:
            print(e)
            pass
        try:
            msg = update.callback_query.message

            # Если текущий экран — медиа-сообщение (например, карточка оборудования с фото),
            # то его нельзя надёжно "перерисовать" через edit_text. По требованиям UX —
            # такие сообщения нужно удалять при любом действии и отправлять новый экран.
            try:
                has_media = bool(getattr(msg, "photo", None) or getattr(msg, "video", None) or getattr(msg, "document", None))
                has_text = bool(getattr(msg, "text", None))
                if msg and has_media and not has_text:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    await update.effective_chat.send_message(
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    return
            except Exception:
                pass

            if msg and _is_inline_notice_message_text(getattr(msg, "text", None)):
                try:
                    await msg.delete()
                except Exception:
                    pass
                await update.effective_chat.send_message(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                return

            if msg and msg.text == text and msg.reply_markup == reply_markup:
                return
            await msg.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            try:
                await update.effective_chat.send_message(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                return
            except Exception as e:
                print(e)
                return

    await update.effective_chat.send_message(
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
    )


async def nav_kb(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> InlineKeyboardMarkup:
    """
    Главное меню (inline-клавиатура).

    Неверифицированным пользователям недоступны корзина, услуги и создание обращений.
    """
    udata = context.user_data or {}
    uid = str(user_id)

    snap = await get_nav_snapshot(
        user_id,
        tg_username=context.user_data.get("tg_username"),
        consent_version_required=CONSENT_VERSION,
    )

    consent_ok = snap["consent_ok"] or snap["is_spec"]  # если хочешь как сейчас: спецам можно без согласия
    verified = snap["verified"]
    phone_confirmed = snap["phone_confirmed"]
    inn_ok = snap["inn_ok"]
    is_spec = snap["is_spec"]
    # Партнёры (profession=5): is_spec=False, но у них есть spec-запись — не показываем "Закончить верификацию"
    _spec = snap.get("spec") or {}
    is_partner_spec = bool(_spec) and int(_spec.get("profession") or 0) == 5

    udata["_consent_ok"] = consent_ok
    udata["_verified"] = verified

    cart = udata.get("cart", []) or []
    cart_len_val = sum(int(x.get("qty") or 1) for x in cart)

    rows: list[list[InlineKeyboardButton]] = []

    def row1(a: InlineKeyboardButton):
        rows.append([a])

    def row2(a: InlineKeyboardButton, b: InlineKeyboardButton):
        rows.append([a, b])

    # Для клиентов показываем все основные разделы всегда;
    # доступ контролируется runtime-guard'ами внутри обработчиков.
    if not is_spec:
        row1(InlineKeyboardButton("🔗 Реферальный QR (получи скидку)", callback_data="my_referral_qr"))
        row1(InlineKeyboardButton("💬 Мои обращения", callback_data="cc_my_chats"))
    row1(InlineKeyboardButton("🛠 Тех. поддержка", callback_data="client_chat_start:1"))
    row1(InlineKeyboardButton("🧰 Услуги", callback_data="services"))
    row1(InlineKeyboardButton("🧾 Бухгалтерия", callback_data="client_chat_start:0"))

    # Баланс баллов — показываем рядом с кнопкой профиля только верифицированным
    _loyalty_badge = ""
    if verified:
        try:
            from services.loyalty_engine import is_loyalty_enabled
            from services.ledger import get_available_balance
            from db.users import resolve_internal_user_id
            if await is_loyalty_enabled():
                _iid = await resolve_internal_user_id(tg_user_id=user_id)
                if _iid:
                    _bal = await get_available_balance("client", _iid)
                    if _bal and _bal > 0:
                        _loyalty_badge = f" | ⭐ {_bal}"
        except Exception:
            pass
    row1(InlineKeyboardButton(f"👤 Мой профиль{_loyalty_badge}", callback_data="profile"))
    if not is_spec:
        row2(
            InlineKeyboardButton(f"{RU_FLAG} Магазин уЮрты", callback_data="buy"),
            InlineKeyboardButton(f"🛒 Корзина ({cart_len_val})", callback_data="cart"),
        )

    # "Мои чаты" — только специалистам
    if is_spec:
        spec = snap.get("spec") or {}
        role = (spec.get("role") or "specialist")
        prof = int(spec.get("profession") or 0)

        # директор видит все непринятые, остальные — только по своему отделу
        try:
            if role == "director":
                pending_cnt = await count_pending_unassigned(profession=None)
                consult_cnt = await count_consultations(profession=None)
                pending_cnt = int(pending_cnt or 0) + int(consult_cnt or 0)
            else:
                pending_cnt = await count_pending_unassigned(profession=prof)
                consult_cnt = await count_consultations(profession=prof)
                pending_cnt = int(pending_cnt or 0) + int(consult_cnt or 0)
        except Exception:
            pending_cnt = 0

        badge = f" ❗️{pending_cnt}" if pending_cnt > 0 else ""
        cb_open = "ag_dir_sections" if role == "director" else "ag_refresh"
        row1(InlineKeyboardButton(f"🧑‍💼 Мои чаты (специалист){badge}", callback_data=cb_open))
        if role == "director":
            row1(InlineKeyboardButton("Заказы", callback_data="users_with_orders"))


    else:

        need_finish = (not phone_confirmed) or (not inn_ok)
        if need_finish and not is_partner_spec:
            row1(InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register"))
    
    
    row1(InlineKeyboardButton("🔙 В меню", callback_data="back_to_main"))
    if is_admin(user_id):
        row1(InlineKeyboardButton("🛠 Админка", callback_data="admin_menu"))

    return InlineKeyboardMarkup(rows)


async def _safe_delete(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(e)
        pass




async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int | None) -> bool:
    return await safe_delete_message_tg(context, chat_id, message_id)


async def send_temporary_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
):
    return await send_temporary_message_tg(update, context, text, reply_markup=reply_markup, parse_mode=parse_mode)


async def cleanup_temporary_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = int(update.effective_chat.id)
    await cleanup_temporary_messages_tg(context, chat_id)

async def show_main_menu(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        title: str = "Чем могу помочь?",
        force_send_new: bool = False,
):
    """
    Рендер главного меню через новый слой ui_flow.
    """
    from db import ui_set_main_menu_mid

    uid = int(update.effective_user.id)
    kb = await nav_kb(context, uid)

    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    used_mid = await _ui.render_screen(
        update,
        context,
        title,
        reply_markup=kb,
        force_new=force_send_new,
    )

    if used_mid:
        context.user_data["_menu_msg_id"] = int(used_mid)
        try:
            await ui_set_main_menu_mid(uid, int(used_mid))
        except Exception:
            pass

    return STATE_IDLE


async def chat_webapp_button(
        *,
        chat_id: int,
        role: str,  # "client" | "agent"
        actor_id: int,  # tg_user_id
        text: str = "💬 Ответить",
) -> InlineKeyboardButton:
    """
    Создаёт ВРЕМЕННЫЙ токен доступа к чату и кнопку WebApp.
    """
    token = await create_chat_access_token(
        chat_id=chat_id,
        role=role,
        actor_id=str(actor_id),
    )

    url = f"{WEBAPP_URL}/chat?t={token}"
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))
