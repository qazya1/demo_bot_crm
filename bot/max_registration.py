"""
max_registration.py — Регистрация, согласие, идентификация, профиль, редактирование для MAX-бота.

Перенос логики из bot_registration.py с адаптацией:
- В MAX поддерживаем подтверждение телефона через request_contact + ручной ввод fallback
- Состояния хранятся в user_data["_state"]
- Вместо Telegram Update/Context используем MaxBotContext/MaxMessage/MaxCallback
"""

import re
import logging
import asyncio
import datetime
import time
from typing import Optional

from max_api import MaxMessage, MaxCallback, InlineKeyboard, InlineButton
from max_bot_main import MaxBotContext
from subscription_check import is_subscribed_max, MAX_REQUIRED_CHANNEL_ID
from message_cleanup import (
    MAX_LAST_MENU_KEY,
    extract_max_mid,
    max_cleanup_temporary_messages,
    max_safe_delete_message,
    max_track_temp_message,
)
from ui_flow import MaxUIFlow, DEFAULT_POLICY, TRACKED_USER_INPUT_IDS_KEY
from notice_center import clear_user_notices_max

# Единый экземпляр MaxUIFlow
_max_ui = MaxUIFlow(DEFAULT_POLICY)

import os
import sys
from pathlib import Path

_bot_dir = Path(__file__).resolve().parent
_project_dir = _bot_dir.parent
sys.path.insert(0, str(_project_dir))

from db import (
    upsert_max_user,
    get_user_by_internal_id,
    update_user_by_internal_id,
    set_user_consent_by_internal_id,
    get_user_consent_by_internal_id,
    mark_user_verified_by_internal_id,
    is_user_verified_by_internal_id,
    wipe_user_max_identity,
    wipe_user_all,
    reset_user_profile_by_internal_id,
    maybe_our_client,
    try_merge_max_user_with_preloaded,
    get_user_codes_by_internal_id,
    get_user_purchases_by_internal_id,
    get_client_by_inn,
    get_client_kkt_slots,
    get_company_owner_kkt_slots_by_inn,
    get_kkt_slots_by_client_id,
    get_user_inn_by_id,
    log_event,
    get_specialist_by_phone,
    is_partner_specialist,
    link_specialist_max_user,
    set_specialist_phone_verified,
    apply_referral_code,
    ensure_partner_entrypoint_chat,
    resolve_partner_identity_by_phone,
)

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────
CONSENT_VERSION = 1
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PAGE_SIZE_CODES = 10
WELCOME_IMAGE_PATH = Path(os.getenv("WELCOME_IMAGE_PATH", "./welcome.png"))

# Состояния (state machine)
ST_IDLE = "idle"
ST_WAIT_NAME = "wait_name"
ST_WAIT_CONSENT = "wait_consent"
ST_WAIT_PHONE = "wait_phone"
ST_WAIT_INN = "wait_inn"
ST_EDIT_FIRSTNAME = "edit_firstname"
ST_EDIT_LASTNAME = "edit_lastname"
ST_EDIT_PHONE = "edit_phone"
ST_EDIT_EMAIL = "edit_email"
ST_EDIT_INN = "edit_inn"
ST_EDIT_CITY = "edit_city"
ST_CO_INN = "co_inn"
ST_CO_KKT_FACTORY_NUMBER = "co_kkt_factory_number"

# Application form states
ST_FORM_TP = "form_tp"
ST_FORM_ADDR = "form_addr"
ST_FORM_ACT = "form_act"
ST_FORM_ASSORT = "form_assort"
ST_FORM_SERVICES = "form_services"
ST_FORM_SNO = "form_sno"
ST_FORM_MARKED = "form_marked"
ST_FORM_FNTERM = "form_fnterm"


async def _require_verified_for_loyalty(
    ctx: MaxBotContext,
    chat_id: str,
    *,
    message_id: str | None = None,
    back_callback: str = "profile",
) -> bool:
    """Лояльность и реферальные разделы доступны только верифицированным пользователям."""
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
        [InlineButton("🔐 Закончить верификацию", callback_data="finish_verify")],
        [InlineButton("👤 Мой профиль", callback_data=back_callback)],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(
        ctx,
        chat_id,
        "⛔ Разделы программы лояльности доступны только после верификации.\n\n"
        "Подтвердите телефон и укажите ИНН.",
        keyboard=kb,
        message_id=message_id,
    )
    return False
ST_FORM_OFD = "form_ofd"
ST_FORM_BANKS = "form_banks"


# ── Утилиты ──────────────────────────────────────────────────────────

def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _normalize_phone(s: str) -> Optional[str]:
    d = _digits(s)
    if len(d) == 10 and d.startswith("9"):
        d = "7" + d
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    if len(d) == 11 and d.startswith("7"):
        return "+" + d
    return None


def _validate_inn_checksum(inn: str) -> bool:
    """Проверка контрольного числа ИНН по алгоритму ФНС."""
    digits = [int(c) for c in inn]
    if len(inn) == 10:
        coeff = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return digits[9] == sum(c * d for c, d in zip(coeff, digits)) % 11 % 10
    if len(inn) == 12:
        coeff11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        coeff12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        ok11 = digits[10] == sum(c * d for c, d in zip(coeff11, digits)) % 11 % 10
        ok12 = digits[11] == sum(c * d for c, d in zip(coeff12, digits)) % 11 % 10
        return ok11 and ok12
    return False


def _normalize_inn(raw: str) -> Optional[str]:
    s = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(s) not in (10, 12):
        return None
    if not _validate_inn_checksum(s):
        return None
    return s


def _looks_like_inn(s: str) -> bool:
    d = _digits(s)
    return len(d) in (10, 12)


def _looks_like_phone(s: str) -> bool:
    d = _digits(s)
    return len(d) >= 10


def _set_state(ctx: MaxBotContext, state: str) -> None:
    old = ctx.user_data.get("_state", ST_IDLE)
    ctx.user_data["_state"] = state
    if old != state:
        logger.debug("state: %s -> %s", old, state)


def _get_state(ctx: MaxBotContext) -> str:
    return ctx.user_data.get("_state", ST_IDLE)


def _save_chat_id(ctx: MaxBotContext, chat_id: str) -> None:
    """Сохраняем chat_id в user_data для повторного использования в callback'ах,
    где MAX API не возвращает message (и chat_id теряется)."""
    if chat_id and str(chat_id).strip():
        ctx.user_data["_chat_id"] = str(chat_id).strip()


def _resolve_chat_id(ctx: MaxBotContext, chat_id: str) -> str:
    """Вернуть chat_id: переданный или сохранённый ранее."""
    cid = str(chat_id or "").strip()
    if cid:
        return cid
    return str(ctx.user_data.get("_chat_id") or "").strip()


async def _ensure_db_id_from_sender(ctx: MaxBotContext, sender_user_id: str) -> int:
    """Гарантированно получить internal user id для текущего sender."""
    db_id = int(ctx.user_data.get("_db_id") or 0)
    if db_id > 0:
        return db_id
    return await _resolve_db_id(ctx, sender_user_id)


async def _resolve_db_id(ctx: MaxBotContext, max_uid: str) -> int:
    """Resolve MAX user ID → internal users.id, кэшируя в user_data."""
    cached = ctx.user_data.get("_db_id")
    if cached:
        return int(cached)
    internal_id = await upsert_max_user(max_uid)
    ctx.user_data["_db_id"] = internal_id
    ctx.user_data["_max_uid"] = max_uid
    return internal_id


async def _has_consent_by_id(db_id: int) -> bool:
    try:
        c = await get_user_consent_by_internal_id(db_id) or {}
        return bool(c.get("consent_accepted_at")) and int(c.get("consent_version", 0)) >= CONSENT_VERSION
    except Exception:
        return False


async def _hydrate_user_cache(ctx: MaxBotContext, db_id: int) -> None:
    """Подтягивает профиль из БД в user_data по internal id."""
    try:
        u = await get_user_by_internal_id(db_id)
        if u:
            for k in ("first_name", "last_name", "phone", "email", "city"):
                if u.get(k) is not None:
                    ctx.user_data[k] = u.get(k)
            inn_db = (u.get("inn") or "")
            if inn_db:
                ctx.user_data["inn"] = inn_db
            ctx.user_data["phone_confirmed"] = bool(u.get("phone"))
            ctx.user_data["_consent_ok"] = bool(u.get("consent_accepted") or u.get("consent_accepted_at"))
            ctx.user_data["_verified"] = bool(u.get("is_verified"))
    except Exception as e:
        logger.warning("hydrate_user_cache failed for db_id=%s: %s", db_id, e)


def _days_badge(days_left: int) -> str:
    if days_left < 0:
        return "⛔ истёк"
    return f"{days_left} дн."


def _fmt_date(raw_date, today: datetime.date) -> str:
    if not raw_date:
        return "нет данных"
    try:
        if isinstance(raw_date, datetime.datetime):
            d = raw_date.date()
        elif isinstance(raw_date, datetime.date):
            d = raw_date
        else:
            d = datetime.date.fromisoformat(str(raw_date).strip()[:10])
        left = (d - today).days
        badge = _days_badge(left)
        return f"{d:%d.%m.%Y} ({badge})"
    except Exception:
        return str(raw_date)


# ── Удаление сообщений / PII cleanup ─────────────────────────────────

async def _safe_delete(api, message_id: str, ctx: MaxBotContext | None = None) -> None:
    """Безопасное удаление сообщения (best effort)."""
    await max_safe_delete_message(api, message_id, ctx=ctx)


def reg_track_mid(ctx: MaxBotContext, message_id: str, *, from_user: bool = False) -> None:
    """Запоминаем message_id, которые надо удалить после окончания регистрации."""
    if not message_id:
        return
    key = "reg_cleanup_mids"
    arr = ctx.user_data.get(key)
    if not isinstance(arr, list):
        arr = []
        ctx.user_data[key] = arr
    if message_id not in arr:
        arr.append(message_id)
    if len(arr) > 80:
        ctx.user_data[key] = arr[-80:]
    # MAX API не позволяет удалять пользовательские message_created.
    # Трекаем только сообщения, отправленные ботом.
    if not from_user:
        max_track_temp_message(ctx, message_id)


async def reg_cleanup_messages(ctx: MaxBotContext, keep_ids: set[str] | None = None) -> None:
    """Удаляем все сообщения из reg_cleanup_mids, кроме keep_ids."""
    keep_ids = keep_ids or set()
    key = "reg_cleanup_mids"
    arr = ctx.user_data.get(key)
    if not isinstance(arr, list) or not arr:
        return

    mids = [x for x in arr if x and x not in keep_ids]
    ctx.user_data.pop(key, None)

    for mid in mids:
        await _safe_delete(ctx.api, mid, ctx=ctx)


_PII_RE = re.compile(r"(\b\d{10,12}\b)")  # ИНН (10/12 цифр) или телефон (10+ цифр)


def _contains_pii(text: str) -> bool:
    """Проверяем, содержит ли текст персональные данные (телефон, ИНН)."""
    if not text:
        return False
    digits = re.sub(r"\D", "", text)
    # Если текст содержит 10+ подряд цифр, скорее всего PII
    return bool(_PII_RE.search(text)) and len(digits) >= 10


async def schedule_pii_cleanup(ctx: MaxBotContext, message_id: str, delay: float = 5.0) -> None:
    """Запланировать удаление сообщения с PII через delay секунд."""
    if not message_id:
        return

    async def _do_delete():
        await asyncio.sleep(delay)
        await _safe_delete(ctx.api, message_id, ctx=ctx)

    asyncio.create_task(_do_delete())


# ── Вспомогательные функции для отправки/редактирования ──────────────


async def _send_or_edit(ctx: MaxBotContext, chat_id: str, text: str,
                        keyboard: Optional[InlineKeyboard] = None,
                        message_id: Optional[str] = None,
                        user_id: Optional[int] = None) -> str:
    """
    Централизованный рендер экранов/меню в MAX через MaxUIFlow.
    """
    return await _max_ui.render_screen(ctx, chat_id, text, reply_markup=keyboard, message_id=message_id)


async def send_temporary_message_max(
    ctx: MaxBotContext,
    chat_id: str,
    text: str,
    *,
    keyboard: Optional[InlineKeyboard] = None,
) -> str:
    """Отправка временного служебного сообщения через MaxUIFlow (notice)."""
    return await _max_ui.render_notice(ctx, chat_id, text, keyboard=keyboard)


def _back_kb() -> InlineKeyboard:
    return InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])


def _profile_back_kb() -> InlineKeyboard:
    return InlineKeyboard(rows=[[InlineButton("⬅️ В профиль", callback_data="profile")]])


async def _send_text(
    ctx: MaxBotContext,
    chat_id: str,
    text: str,
    *,
    keyboard: Optional[InlineKeyboard] = None,
) -> dict:
    """Отправить сообщение в chat_id, а если его нет — пользователю по user_id."""
    target_chat_id = str(chat_id or "").strip()
    if target_chat_id:
        return await ctx.api.send_message(target_chat_id, text, keyboard=keyboard)

    uid = str(ctx.user_data.get("_max_uid") or "").strip()
    if not uid and ctx.user_id:
        uid = str(ctx.user_id).strip()
    if uid:
        return await ctx.api.send_message_to_user(uid, text, keyboard=keyboard)

    logger.warning("send_text: no chat_id and no _max_uid in user_data")
    return {}


async def _send_and_track(
    ctx: MaxBotContext,
    chat_id: str,
    text: str,
    *,
    keyboard: Optional[InlineKeyboard] = None,
) -> str:
    """
    Отправить сообщение как notice (временное) через MaxUIFlow.
    """
    return await _max_ui.render_notice(ctx, chat_id, text, keyboard=keyboard)


# ══════════════════════════════════════════════════════════════════════
# Проверка подписки на канал
# ══════════════════════════════════════════════════════════════════════

async def _show_subscription_required_max(ctx: MaxBotContext, chat_id: str,
                                          message_id: Optional[str] = None) -> None:
    """Показывает экран «подпишитесь на канал» в MAX."""
    channel_id = MAX_REQUIRED_CHANNEL_ID
    link = f"https://max.ru/join/{channel_id}" if not channel_id.startswith("http") else channel_id
    kb = InlineKeyboard(rows=[
        [InlineButton("📢 Подписаться на канал", url=link)],
        [InlineButton("✅ Я подписался", callback_data="check_subscription")],
    ])
    await _send_or_edit(
        ctx,
        chat_id,
        "📢 Для использования бота необходимо подписаться на наш канал.\n\n"
        "1️⃣ Нажмите «Подписаться на канал»\n"
        "2️⃣ Нажмите «Я подписался» для продолжения",
        keyboard=kb,
        message_id=message_id,
    )
    _set_state(ctx, ST_IDLE)


async def handle_check_subscription_max(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback «Я подписался» — перепроверяет подписку в MAX."""
    if not cb.user:
        return
    max_uid = int(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    subscribed = await is_subscribed_max(ctx.api, max_uid)
    if not subscribed:
        channel_id = MAX_REQUIRED_CHANNEL_ID
        link = f"https://max.ru/join/{channel_id}" if not channel_id.startswith("http") else channel_id
        kb = InlineKeyboard(rows=[
            [InlineButton("📢 Подписаться", url=link)],
            [InlineButton("✅ Я подписался", callback_data="check_subscription")],
        ])
        await _send_or_edit(
            ctx,
            chat_id,
            "❌ Подписка не обнаружена. Пожалуйста, подпишитесь и нажмите «Я подписался» снова.",
            keyboard=kb,
            message_id=mid,
        )
        return

    await _start_registration(ctx, chat_id, message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# /start — точка входа
# ══════════════════════════════════════════════════════════════════════

async def handle_start(ctx: MaxBotContext, msg: MaxMessage) -> None:
    """Команда /start — приветствие и начало регистрации."""
    if not msg.sender:
        return

    max_uid = str(msg.sender.user_id)
    username = msg.sender.username or ""
    name = msg.sender.name or ""

    parts = name.split(maxsplit=1)
    first_name = parts[0] if parts else username or max_uid
    last_name = parts[1] if len(parts) > 1 else ""

    # Сохраняем chat_id для повторного использования в callback'ах
    _save_chat_id(ctx, msg.chat_id)
    try:
        await clear_user_notices_max(
            max_api=ctx.api,
            max_user_id=int(msg.sender.user_id),
        )
    except Exception:
        logger.debug("best-effort MAX /start notice cleanup failed for uid=%s", msg.sender.user_id, exc_info=True)

    # Очищаем «сессионные» данные
    for k in ("cart", "form", "co_mode", "finish_verify_mode",
              "extra_verify_offer", "extra_verify_done",
              "_last_menu_mid"):
        ctx.user_data.pop(k, None)

    # Сохраняем пользователя в БД (MAX) — получаем internal users.id
    try:
        db_id = await upsert_max_user(max_uid, first_name=first_name, last_name=last_name)
        ctx.user_data["_db_id"] = db_id
        ctx.user_data["_max_uid"] = max_uid
    except Exception as e:
        logger.error("upsert_max_user error: %s", e)
        db_id = 0

    try:
        await log_event(event_type="command", internal_user_id=db_id, name="/start",
                        payload={"source": "max", "username": username})
    except Exception:
        pass

    await _hydrate_user_cache(ctx, db_id)

    # Парсим payload (/start ref_1111) и пытаемся зафиксировать реферала, как в TG.
    payload = ""
    try:
        text = (msg.text or "").strip()
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
    except Exception:
        payload = ""

    if payload.startswith("ref_"):
        ref_code = payload[4:].strip()
        if ref_code and db_id:
            try:
                res = await apply_referral_code(
                    raw_code=ref_code,
                    raw_text=payload,
                    internal_id=db_id,
                    source_platform="max",
                )
                if res.get("ok") and res.get("status") in ("applied", "already_set"):
                    ctx.user_data["_ref_thanks_pending"] = True
            except Exception:
                logger.exception("MAX start referral apply failed: db_id=%s payload=%s", db_id, payload)

    # Приветствие (как в TG: hero-картинка + пауза, не чаще раза в минуту)
    now = time.time()
    last_welcome = float(ctx.user_data.get("_welcome_sent_ts") or 0)
    sent_welcome = False
    if now - last_welcome > 60:
        ctx.user_data["_welcome_sent_ts"] = now
        sent_welcome = await _send_welcome_hero(ctx, msg.chat_id, first_name)

    if sent_welcome:
        await asyncio.sleep(2)

    # Проверка подписки на канал (если MAX_REQUIRED_CHANNEL_ID задан)
    if MAX_REQUIRED_CHANNEL_ID:
        max_uid_int = int(msg.sender.user_id) if msg.sender else 0
        if max_uid_int and not await is_subscribed_max(ctx.api, max_uid_int):
            await _show_subscription_required_max(ctx, msg.chat_id)
            return

    # Запускаем поток регистрации
    await _start_registration(ctx, msg.chat_id)


async def _send_welcome_hero(ctx: MaxBotContext, chat_id: str, first_name: str) -> bool:
    caption = (
        "Давайте знакомиться, меня зовут Юрта_бот!\n\n"
        f"Здравствуйте, {first_name}! 👋\n\n"
        "Я бот компании «уЮрта» — помогу с кассовым оборудованием, "
        "бухгалтерией и техподдержкой."
    )
    try:
        if WELCOME_IMAGE_PATH.exists():
            with open(WELCOME_IMAGE_PATH, "rb") as f:
                data = f.read()
            result = await ctx.api.send_photo(chat_id, data, caption=caption, filename=WELCOME_IMAGE_PATH.name)
            # Track the welcome message for cleanup
            msg_obj = result.get("message")
            if isinstance(msg_obj, dict):
                mid = msg_obj.get("body", {}).get("mid", "")
                if mid:
                    reg_track_mid(ctx, mid)
            return True

        await _send_and_track(ctx, chat_id, caption)
        return True
    except Exception as e:
        logger.warning("send_welcome_hero failed: %s", e)
        try:
            await _send_and_track(ctx, chat_id, caption)
            return True
        except Exception:
            return False




async def handle_wipe_me(ctx: MaxBotContext, msg: MaxMessage) -> None:
    """Команда /wipe_me — отвязать MAX-профиль и очистить данные регистрации (для тестов)."""
    if not msg.sender:
        return

    max_uid = str(msg.sender.user_id)
    try:
        stats = await wipe_user_max_identity(max_uid)
    except Exception as e:
        logger.exception("wipe_me failed for max_uid=%s: %s", max_uid, e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось очистить профиль. Проверьте БД и повторите.")
        return

    ctx.user_data.clear()
    ctx.user_data["_max_uid"] = max_uid

    kb = InlineKeyboard(rows=[
        [InlineButton("📝 Начать регистрацию", callback_data="register")],
    ])
    await _send_text(
        ctx,
        msg.chat_id,
        f"Готово. MAX-профиль отвязан (обновлено записей: {stats.get('users_updated', 0)}).\n"
        "Нажмите «Начать регистрацию».",
        keyboard=kb,
    )


async def handle_delete_me(ctx: MaxBotContext, msg: MaxMessage) -> None:
    """Команда /delete_me — полное удаление пользователя (hard delete)."""
    if not msg.sender:
        return

    max_uid = str(msg.sender.user_id)
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception as e:
        logger.exception("delete_me resolve_db_id failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Временная ошибка подключения к базе.")
        return

    try:
        # ВАЖНО: wipe_user_all принимает positional user_id как tg_user_id (legacy),
        # поэтому для MAX нужно передавать внутренний id именованным аргументом.
        stats = await wipe_user_all(internal_user_id=db_id)
    except Exception as e:
        logger.exception("delete_me failed for db_id=%s: %s", db_id, e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось удалить аккаунт. Проверьте БД и повторите.")
        return

    ctx.user_data.clear()
    ctx.user_data["_max_uid"] = max_uid

    try:
        await log_event(event_type="command", internal_user_id=db_id, name="/delete_me",
                        payload={"source": "max"})
    except Exception:
        pass

    kb = InlineKeyboard(rows=[
        [InlineButton("📝 Начать регистрацию", callback_data="register")],
    ])
    await _send_text(
        ctx,
        msg.chat_id,
        f"Готово. Освобождено кодов: {stats.get('codes_reset', 0)}, "
        f"удалено заказов: {stats.get('orders_deleted', 0)}, "
        f"удалено пользователей: {stats.get('users_deleted', 0)}.\n"
        "Нажмите «Начать регистрацию».",
        keyboard=kb,
    )


# ══════════════════════════════════════════════════════════════════════
# Основная state-машина регистрации
# ══════════════════════════════════════════════════════════════════════

async def _start_registration(ctx: MaxBotContext, chat_id: str,
                               message_id: Optional[str] = None) -> None:
    """
    Центральный роутер регистрации — аналог start_registration из TG-бота.
    Проверяет что ещё не заполнено и переходит к нужному шагу.
    """
    # ── Idempotency guard: не отправляем сообщения одного шага повторно ──
    now = time.time()
    last_step_ts = float(ctx.user_data.get("_reg_step_ts") or 0)
    last_step_name = ctx.user_data.get("_reg_step_name") or ""

    chat_id = _resolve_chat_id(ctx, chat_id)

    db_id = ctx.user_data.get("_db_id", 0)
    await _hydrate_user_cache(ctx, db_id)

    phone_conf = bool(ctx.user_data.get("phone_confirmed"))
    inn = (ctx.user_data.get("inn") or "").strip()

    # Если телефон есть, ИНН пропущен, и не в режиме «дозавершить» — сразу меню
    if phone_conf and ctx.user_data.get("skipped_inn") and not ctx.user_data.get("finish_verify_mode"):
        _set_state(ctx, ST_IDLE)
        await _show_main_menu(ctx, chat_id,
                              "Вы уже можете пользоваться меню. ИНН можно указать позже через «Мой профиль».",
                              message_id=message_id)
        return

    # 1) Согласие на ОПД
    if not (await _has_consent_by_id(db_id)):
        if last_step_name == "consent" and (now - last_step_ts) < 10:
            logger.debug("_start_registration: skip duplicate consent step")
            return
        ctx.user_data["_reg_step_ts"] = now
        ctx.user_data["_reg_step_name"] = "consent"
        await _show_consent(ctx, chat_id, message_id=message_id)
        return

    # 2) Имя
    if not (ctx.user_data.get("first_name") or "").strip():
        if last_step_name == "name" and (now - last_step_ts) < 10:
            logger.debug("_start_registration: skip duplicate name step")
            return
        ctx.user_data["_reg_step_ts"] = now
        ctx.user_data["_reg_step_name"] = "name"
        thanks_prefix = "✅ Спасибо за доверие, продолжаем.\n\n" if ctx.user_data.pop("_show_thanks_once", False) else ""
        await _send_or_edit(ctx, chat_id, f"{thanks_prefix}Как вас зовут?", message_id=message_id)
        _set_state(ctx, ST_WAIT_NAME)
        return

    # 3) Телефон
    if not phone_conf:
        if last_step_name == "phone" and (now - last_step_ts) < 10:
            logger.debug("_start_registration: skip duplicate phone step")
            return
        ctx.user_data["_reg_step_ts"] = now
        ctx.user_data["_reg_step_name"] = "phone"
        _set_state(ctx, ST_WAIT_PHONE)
        thanks_prefix = "✅ Спасибо за доверие, продолжаем.\n\n" if ctx.user_data.pop("_show_thanks_once", False) else ""
        phone_kb = InlineKeyboard(rows=[
            [InlineButton("📱 Подтвердить номер из MAX", request_contact=True)],
        ])
        await _send_or_edit(
            ctx,
            chat_id,
            f"{thanks_prefix}Для регистрации нужно подтвердить номер телефона и указать ИНН.\n\n"
            "Нажмите кнопку ниже, чтобы отправить номер из вашего MAX-профиля,\n"
            "или введите его вручную в формате +79001234567:",
            keyboard=phone_kb,
        )
        return

    # 4) ИНН
    if not inn:
        if last_step_name == "inn" and (now - last_step_ts) < 10:
            logger.debug("_start_registration: skip duplicate inn step")
            return
        ctx.user_data["_reg_step_ts"] = now
        ctx.user_data["_reg_step_name"] = "inn"
        kb = InlineKeyboard(rows=[
            [InlineButton("❌ Ввести позже", callback_data="skip_inn")],
        ])
        # Устанавливаем состояние ДО sleep, чтобы если пользователь
        # быстро напишет ИНН — он был обработан корректно
        _set_state(ctx, ST_WAIT_INN)
        thanks_prefix = "✅ Спасибо за доверие, продолжаем.\n\n" if ctx.user_data.pop("_show_thanks_once", False) else ""
        await _send_and_track(ctx, chat_id, f"{thanks_prefix}Давайте проверим, работали мы с вами ранее или нет.")
        await asyncio.sleep(2)
        mid = await _send_or_edit(ctx, chat_id,
                            "Для этого нам понадобится ваш ИНН (10 или 12 цифр).\n\n"
                            "Если ИНН сейчас под рукой нет, нажмите «Ввести позже».",
                            keyboard=kb, message_id=message_id)
        if mid:
            reg_track_mid(ctx, mid)
        return

    # 5) Финал — всё заполнено
    ctx.user_data.pop("finish_verify_mode", None)
    await _verify_and_finish(ctx, chat_id, message_id=message_id)


# ══════════════════════════════════════════════════════════════════════
# Согласие на ОПД
# ══════════════════════════════════════════════════════════════════════

async def _show_consent(ctx: MaxBotContext, chat_id: str, message_id: Optional[str] = None) -> None:
    text = (
        "Регистрируясь в сервисе, вы подтверждаете согласие на обработку, "
        "передачу и хранение ваших персональных данных, а также на получение "
        "сервисных уведомлений."
    )
    kb = InlineKeyboard(rows=[
        [InlineButton("✅ Подтверждаю", callback_data="accept_consent")],
        [InlineButton("❌ Продолжить без регистрации", callback_data="go_guest")],
        [InlineButton("⬅️ В меню", callback_data="back_to_main")],
    ])
    mid = await _send_or_edit(ctx, chat_id, text, keyboard=kb, message_id=message_id)
    if mid:
        reg_track_mid(ctx, mid)
    _set_state(ctx, ST_WAIT_CONSENT)


async def handle_accept_consent(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «Подтверждаю» — сохраняем согласие и продолжаем."""
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception as e:
        logger.exception("accept_consent resolve_db_id failed: %s", e)
        ctx.user_data.setdefault("_max_uid", max_uid)
        await _send_and_track(ctx, chat_id, "⚠️ Временная ошибка подключения к базе. Проверьте настройки БД и повторите.")
        return

    try:
        await set_user_consent_by_internal_id(db_id)
        ctx.user_data["_consent_ok"] = True
        ctx.user_data.pop("_reg_step_name", None)
        ctx.user_data.pop("_reg_step_ts", None)
        logger.info("consent_accept: db_id=%s, version=%s, source=max", db_id, CONSENT_VERSION)
    except Exception as e:
        logger.warning("set_user_consent failed: %s", e)

    mid = cb.message.message_id if cb.message else None
    if mid:
        try:
            # Надёжно убираем старое сообщение согласия вместе с кнопками.
            await ctx.api.delete_message(mid)
        except Exception:
            pass
    await _send_and_track(ctx, chat_id, "✅ Спасибо за доверие, продолжаем.")

    # Показываем благодарность на следующем экране регистрации (без авто-удаления notice).
    ctx.user_data["_show_thanks_once"] = True

    await _start_registration(ctx, chat_id)


async def handle_go_guest(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «Продолжить без регистрации» — гостевой режим."""
    if not cb.user:
        return
    ctx.user_data["_consent_ok"] = False
    _set_state(ctx, ST_IDLE)

    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    if mid:
        try:
            await ctx.api.delete_message(mid)
        except Exception:
            pass
    await _show_guest_menu(ctx, chat_id, message_id=None)


async def _show_guest_menu(ctx: MaxBotContext, chat_id: str, message_id: Optional[str] = None) -> None:
    kb = InlineKeyboard(rows=[
        [InlineButton("💬 Мои обращения", callback_data="my_chats"),
         InlineButton("🛒 Корзина", callback_data="cart")],
        [InlineButton("🧰 Услуги", callback_data="services")],
        [InlineButton("🧾 Бухгалтерия / Отчётность", callback_data="client_chat_start:0")],
        [InlineButton("🇷🇺 Магазин уЮрты", callback_data="buy")],
        [InlineButton("🛠 Техническая поддержка", callback_data="client_chat_start:1")],
        [InlineButton("👤 Мой профиль", callback_data="profile")],
        [InlineButton("🔐 Зарегистрироваться", callback_data="register")],
    ])
    await _send_or_edit(ctx, chat_id,
                        "Вы в гостевом режиме. Каталог доступен.\n"
                        "Когда решите оформить покупку — подскажем, какие данные нужно подтвердить.",
                        keyboard=kb, message_id=message_id)


# ══════════════════════════════════════════════════════════════════════
# Обработка текстовых сообщений по state
# ══════════════════════════════════════════════════════════════════════

async def handle_text_by_state(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """
    Обрабатывает текстовые сообщения в зависимости от текущего state.
    Возвращает True, если сообщение обработано (state != idle).
    """
    if not msg.sender:
        return False

    state = _get_state(ctx)

    if state == ST_WAIT_NAME:
        return await _on_name(ctx, msg)

    # ── PII cleanup: отслеживаем сообщения с телефоном/ИНН ──
    if state in (ST_WAIT_PHONE, ST_WAIT_INN, ST_EDIT_PHONE, ST_EDIT_INN, ST_CO_INN, ST_CO_KKT_FACTORY_NUMBER):
        user_mid = getattr(msg, "message_id", None) or ""
        if user_mid:
            reg_track_mid(ctx, str(user_mid), from_user=True)

    if state == ST_WAIT_PHONE:
        return await _on_phone(ctx, msg)

    if state == ST_WAIT_INN:
        return await _on_inn(ctx, msg)

    # Редактирование профиля
    if state == ST_EDIT_FIRSTNAME:
        return await _save_firstname(ctx, msg)
    if state == ST_EDIT_LASTNAME:
        return await _save_lastname(ctx, msg)
    if state == ST_EDIT_PHONE:
        return await _save_phone(ctx, msg)
    if state == ST_EDIT_EMAIL:
        return await _save_email(ctx, msg)
    if state == ST_EDIT_INN:
        return await _save_inn(ctx, msg)
    if state == ST_EDIT_CITY:
        return await _save_city(ctx, msg)
    
    # Checkout flow states
    if state == ST_CO_INN:
        return await _save_inn(ctx, msg)
    if state == ST_CO_KKT_FACTORY_NUMBER:
        return await _save_kkt_factory_number(ctx, msg)

    # Application form states
    if state == ST_FORM_TP:
        return await _form_tp(ctx, msg)
    if state == ST_FORM_ADDR:
        return await _form_addr(ctx, msg)
    if state == ST_FORM_ASSORT:
        return await _form_assort(ctx, msg)
    if state == ST_FORM_BANKS:
        return await _form_banks(ctx, msg)

    return False


# ── Ввод имени ────────────────────────────────────────────────────────

async def _on_name(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    name = (msg.text or "").strip()
    if not name:
        await _send_and_track(ctx, msg.chat_id, "Пожалуйста, введите ваше имя:")
        return True
    ctx.user_data["first_name"] = name
    ctx.user_data.pop("_reg_step_name", None)
    ctx.user_data.pop("_reg_step_ts", None)
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, first_name=name)
        await _try_award_profile_bonus(db_id, "first_name")
    except Exception as e:
        logger.exception("save first_name failed: %s", e)
        await _send_and_track(ctx, msg.chat_id, "⚠️ Не удалось сохранить имя. Попробуйте ещё раз.")
        return True

    await _start_registration(ctx, msg.chat_id)
    return True


# ── Ввод телефона ─────────────────────────────────────────────────────

async def _on_phone(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    raw_contact_phone = (msg.contact_phone or "").strip()
    text = (msg.text or "").strip()
    phone = _normalize_phone(raw_contact_phone or text)

    if not phone:
        phone_kb = InlineKeyboard(rows=[
            [InlineButton("📱 Отправить номер из MAX", request_contact=True)],
        ])
        await _send_and_track(
            ctx,
            msg.chat_id,
            "Не удалось распознать номер. Нажмите кнопку для отправки номера из MAX "
            "или введите вручную в формате +79001234567:",
            keyboard=phone_kb,
        )
        return True

    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, phone=phone)
    except Exception as e:
        logger.exception("save phone failed: %s", e)
        await _send_and_track(ctx, msg.chat_id, "⚠️ Не удалось сохранить номер. Повторите ввод.")
        return True

    ctx.user_data["phone"] = phone
    ctx.user_data["phone_confirmed"] = True
    ctx.user_data.pop("_reg_step_name", None)
    ctx.user_data.pop("_reg_step_ts", None)
    await _try_award_profile_bonus(db_id, "phone")

    # Сбрасываем _last_menu_mid — следующий промпт (ИНН) должен
    # появиться как новое сообщение внизу чата, а не редактировать
    # старое сообщение с кнопкой request_contact выше
    ctx.user_data.pop("_last_menu_mid", None)

    first_name = (ctx.user_data.get("first_name") or "").strip() or "Коллега"
    specialist = None
    try:
        specialist = await get_specialist_by_phone(phone)
    except Exception as e:
        logger.warning("specialist by phone lookup failed: %s", e)

    if specialist and not is_partner_specialist(specialist):
        try:
            await set_specialist_phone_verified(int(specialist["id"]), True)
            await link_specialist_max_user(
                int(specialist["id"]),
                int(msg.sender.user_id),
                (msg.sender.username or None),
            )
        except Exception as e:
            logger.warning("specialist max link/verify failed: %s", e)

        _set_state(ctx, ST_IDLE)
        await _send_and_track(
            ctx,
            msg.chat_id,
            f"Номер подтверждён ✅ {phone}\n\n"
            f"{first_name}, вы вошли как специалист. Открываю список обращений…"
        )
        try:
            from max_agent_chat import _render_chat_list, _set_agent_ctx as _max_set_agent_ctx  # локальный импорт во избежание циклической зависимости
            _max_set_agent_ctx(ctx, specialist, int(msg.sender.user_id))
            await _render_chat_list(ctx, msg.chat_id)
        except Exception as e:
            logger.exception("specialist mode start failed: %s", e)
            await _show_main_menu(ctx, msg.chat_id, "Не удалось открыть режим специалиста. Доступно обычное меню.")
        return True

    if specialist and is_partner_specialist(specialist):
        logger.info(
            "MAX registration: partner detected by phone, forcing client mode. specialist_id=%s profession=%s max_user_id=%s",
            specialist.get("id"),
            specialist.get("profession"),
            msg.sender.user_id if msg.sender else None,
        )

    # Для партнёра (через is_partner_specialist ИЛИ через linked_user_id/phone в БД)
    # гарантируем постоянный чат с директором сразу при подтверждении телефона.
    # Вызываем всегда когда specialist=None или это партнёр — не обычный специалист.
    if not specialist or is_partner_specialist(specialist):
        try:
            _identity = await resolve_partner_identity_by_phone(
                internal_user_id=db_id if db_id else None,
                max_user_id=int(msg.sender.user_id),
                phone=phone,
            )
            if _identity.get("partner_detected"):
                _canonical_uid = int(_identity.get("canonical_user_id") or 0)
                if _canonical_uid > 0:
                    await ensure_partner_entrypoint_chat(
                        partner_user_id=_canonical_uid,
                        max_user_id=int(msg.sender.user_id),
                        phone=phone,
                        source="max_registration",
                    )
                    logger.info(
                        "MAX _on_phone: partner entrypoint chat ensured canonical_uid=%s",
                        _canonical_uid,
                    )
        except Exception as _pce:
            logger.warning("MAX _on_phone: partner entrypoint chat setup failed: %s", _pce)

    await _send_and_track(
        ctx,
        msg.chat_id,
        f"Номер подтверждён ✅ {phone}"
    )
    await asyncio.sleep(1)
    await _send_and_track(
        ctx,
        msg.chat_id,
        f"{first_name}, спасибо за доверие, продолжим."
    )
    await asyncio.sleep(2)

    await _start_registration(ctx, msg.chat_id)
    return True


# ── Ввод ИНН ──────────────────────────────────────────────────────────

async def _on_inn(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    text = (msg.text or "").strip()

    if not _looks_like_inn(text):
        await _send_and_track(ctx, msg.chat_id, "ИНН должен содержать 10 или 12 цифр. Попробуйте снова.")
        return True

    inn = _digits(text)
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, inn=inn)
    except Exception as e:
        logger.exception("save inn failed: %s", e)
        await _send_and_track(ctx, msg.chat_id, "⚠️ Не удалось сохранить ИНН. Попробуйте снова.")
        return True

    ctx.user_data["inn"] = inn
    ctx.user_data.pop("skipped_inn", None)
    ctx.user_data.pop("_reg_step_name", None)
    ctx.user_data.pop("_reg_step_ts", None)
    await _try_award_profile_bonus(db_id, "inn")
    await _send_and_track(ctx, msg.chat_id, "✅ ИНН принят!")
    await asyncio.sleep(2)
    await _start_registration(ctx, msg.chat_id)
    return True


# ── Пропуск ИНН ──────────────────────────────────────────────────────

async def handle_skip_inn(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «Ввести позже»."""
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    ctx.user_data["skipped_inn"] = True
    _set_state(ctx, ST_IDLE)

    first_name = (ctx.user_data.get("first_name") or "").strip() or "Коллега"
    text = (
        "Благодарим за выбор нашей компании.\n\n"
        f"{first_name}, далее выбирайте интересующий вас пункт меню, расположенного ниже.\n\n"
        "Чем ещё могу вам помочь?"
    )
    await _show_main_menu(ctx, chat_id, text, message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# Верификация и финал
# ══════════════════════════════════════════════════════════════════════

async def _verify_and_finish(ctx: MaxBotContext, chat_id: str,
                              message_id: Optional[str] = None) -> None:
    """Финальный шаг регистрации — верификация и merge с карточкой клиента."""
    db_id = ctx.user_data.get("_db_id", 0)
    max_uid = ctx.user_data.get("_max_uid", "0")
    phone = (ctx.user_data.get("phone") or "").strip()
    inn = (ctx.user_data.get("inn") or "").strip()
    confirmed = bool(ctx.user_data.get("phone_confirmed"))

    first_name = (ctx.user_data.get("first_name") or "").strip() or "Коллега"

    text_verified = (
        "✅ Верификация завершена.\n\n"
        f"{first_name}, далее выбирайте интересующий вас пункт меню, расположенного ниже.\n\n"
        "Чем ещё могу вам помочь?"
    )

    if not confirmed or not inn:
        _set_state(ctx, ST_IDLE)
        await _show_main_menu(ctx, chat_id, text_verified, message_id=message_id)
        return

    # Пытаемся свести с карточкой клиента
    merge_note = None
    try:
        merge_res = await try_merge_max_user_with_preloaded(db_id, int(max_uid), inn, phone)
        st = (merge_res or {}).get("status")
        note = (merge_res or {}).get("note")
        if st == "merged":
            # Обновляем db_id — после merge пользователь может быть в другой строке
            new_id = merge_res.get("target_id")
            if new_id:
                ctx.user_data["_db_id"] = int(new_id)
                db_id = int(new_id)
            merge_note = "🔗 Профиль синхронизирован с вашей карточкой клиента."
        elif st == "mismatch_inn":
            merge_note = "⚠️ В базе найден клиент с таким ИНН, но другим телефоном. Проверьте телефон в профиле."
        elif st == "mismatch_phone":
            merge_note = "⚠️ В базе найден клиент с таким телефоном, но другим ИНН. Проверьте ИНН в профиле."
        elif st == "ambiguous":
            merge_note = "⚠️ Найдено несколько совпадений в базе. Напишите в поддержку для ручной проверки."
        elif st == "conflict":
            merge_note = "⚠️ Эти данные уже привязаны к другому аккаунту. Напишите в поддержку."
        elif note:
            merge_note = str(note)
    except Exception as e:
        logger.exception("try_merge error: %s", e)

    # Ставим verified + consent
    try:
        await mark_user_verified_by_internal_id(db_id)
    except Exception as e:
        logger.exception("mark_user_verified failed: %s", e)

    try:
        await set_user_consent_by_internal_id(db_id)
    except Exception as e:
        logger.exception("set_user_consent failed: %s", e)

    ctx.user_data["_verified"] = True
    ctx.user_data["_consent_ok"] = True

    await _hydrate_user_cache(ctx, db_id)

    final_text = text_verified
    if merge_note:
        final_text = f"{merge_note}\n\n{final_text}"

    _set_state(ctx, ST_IDLE)
    await _show_main_menu(ctx, chat_id, final_text, message_id=message_id)


# ══════════════════════════════════════════════════════════════════════
# Кнопка «Зарегистрироваться» / «Закончить верификацию»
# ══════════════════════════════════════════════════════════════════════

async def handle_register(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback 'register'/'finish_verify' — начать/продолжить регистрацию."""
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception as e:
        logger.exception("accept_consent resolve_db_id failed: %s", e)
        ctx.user_data.setdefault("_max_uid", max_uid)
        await _send_text(ctx, chat_id, "⚠️ Временная ошибка подключения к базе. Проверьте настройки БД и повторите.")
        return
    mid = cb.message.message_id if cb.message else None

    await _hydrate_user_cache(ctx, db_id)
    db_verified = False
    try:
        db_verified = await is_user_verified_by_internal_id(db_id)
    except Exception as e:
        logger.warning("is_user_verified check failed: %s", e)
    already_verified = (db_verified or bool(ctx.user_data.get("_verified"))) and bool((ctx.user_data.get("inn") or "").strip()) and bool(ctx.user_data.get("phone_confirmed"))
    if already_verified:
        await _show_main_menu(
            ctx,
            chat_id,
            "✅ Ваш профиль уже верифицирован. Можете пользоваться всеми разделами меню.",
            message_id=mid,
        )
        return

    ctx.user_data["finish_verify_mode"] = True
    await _start_registration(ctx, chat_id, message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# Профиль
# ══════════════════════════════════════════════════════════════════════

async def handle_profile(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «Мой профиль» — полный экран профиля."""
    if not cb.user:
        return

    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception as e:
        logger.exception("accept_consent resolve_db_id failed: %s", e)
        ctx.user_data.setdefault("_max_uid", max_uid)
        await _send_text(ctx, chat_id, "⚠️ Временная ошибка подключения к базе. Проверьте настройки БД и повторите.")
        return
    mid = cb.message.message_id if cb.message else None

    await _hydrate_user_cache(ctx, db_id)

    profile = {}
    try:
        profile = await get_user_by_internal_id(db_id) or {}
    except Exception as e:
        logger.error("get_user error: %s", e)

    first_name = (profile.get("first_name") or ctx.user_data.get("first_name") or "").strip()
    last_name = (profile.get("last_name") or ctx.user_data.get("last_name") or "").strip()
    email = (profile.get("email") or ctx.user_data.get("email") or "").strip()
    city = (profile.get("city") or ctx.user_data.get("city") or "").strip()
    inn = (profile.get("inn") or ctx.user_data.get("inn") or "").strip()
    phone = (profile.get("phone") or ctx.user_data.get("phone") or "").strip()

    verified = await is_user_verified_by_internal_id(db_id)

    # Блок сроков ККТ
    kkt_block = "нет данных"
    if verified:
        try:
            internal_id = profile.get("id")
            kkt_rows = (await get_kkt_slots_by_client_id(internal_id)) if internal_id else []
            # Фолбэк: если кассы не нашлись по client_id — ищем через ИНН+телефон компании
            # (для старых касс, где владелец ещё не выбран явно)
            if not kkt_rows and inn and phone:
                _, kkt_rows = await get_company_owner_kkt_slots_by_inn(inn, phone)
                kkt_rows = kkt_rows or []
            logger.info(
                "profile_kkt_access platform=max max_user_id=%s internal_id=%s kkt_count=%s",
                max_uid, internal_id, len(kkt_rows),
            )
            if kkt_rows:
                today = datetime.date.today()
                slot_blocks = []
                for kkt in kkt_rows:
                    slot = kkt.get("kkt_slot") or "тт.1"
                    addr = kkt.get("address")
                    ofd_date = kkt.get("ofd_expires_at")
                    fn_date = kkt.get("fn_expires_at")
                    mark_date = kkt.get("mark_expires_at")
                    if not ofd_date and not fn_date and not mark_date:
                        continue
                    lines = []
                    header = f"📍 {slot}"
                    if addr:
                        header += f" — {addr}"
                    lines.append(header)
                    if ofd_date:
                        lines.append(f"  ОФД: {_fmt_date(ofd_date, today)}")
                    if fn_date:
                        lines.append(f"  ФН: {_fmt_date(fn_date, today)}")
                    if mark_date:
                        lines.append(f"  Маркировка: {_fmt_date(mark_date, today)}")
                    slot_blocks.append("\n".join(lines))
                if slot_blocks:
                    kkt_block = "\n\n".join(slot_blocks)
        except Exception as e:
            logger.error("kkt_block error: %s", e)

    # Купленные коды
    purchased_ofd = "—"
    purchased_service = "—"
    purchased_edo = "—"
    try:
        rows_codes = await get_user_codes_by_internal_id(internal_user_id=db_id, limit=50, offset=0)
        now_dt = datetime.datetime.now()
        cutoff_dt = now_dt - datetime.timedelta(days=93)

        buckets = {"ofd": [], "service": [], "edo": []}
        for r in (rows_codes or []):
            itype = (r.get("item_type") or r.get("code_type") or "").lower()
            kind = None
            if itype in ("ofd", "ofd_first", "ofd_platform", "ofd_other") or itype.startswith("ofd_ext:"):
                kind = "ofd"
            elif itype == "service" or itype.startswith("service_ext:") or itype == "service_order":
                kind = "service"
            elif itype == "edo" or itype.startswith("edo_ext:"):
                kind = "edo"
            else:
                continue

            title2 = (r.get("vendor_name") or r.get("item_title") or "").strip()
            if not title2:
                title2 = "ОФД" if kind == "ofd" else ("Сервис" if kind == "service" else "ЭДО")
            code_val = (r.get("code") or "").strip()
            exp = r.get("expires_at")
            sort_dt = datetime.datetime.max
            date_part = ""

            if exp:
                try:
                    exp_dt = exp
                    if isinstance(exp, str):
                        exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", ""))
                    if isinstance(exp_dt, datetime.date) and not isinstance(exp_dt, datetime.datetime):
                        exp_dt = datetime.datetime.combine(exp_dt, datetime.time.min)
                    if exp_dt < cutoff_dt:
                        continue
                    days_left = (exp_dt.date() - now_dt.date()).days
                    date_str = exp_dt.strftime("%d.%m.%Y")
                    if days_left < 0:
                        date_part = f"до {date_str} ⛔ истёк"
                    else:
                        date_part = f"до {date_str} ({days_left} дн.)"
                    sort_dt = exp_dt
                except Exception:
                    pass
            else:
                date_part = "бессрочно"

            line = f"• {title2}: {date_part}"
            if code_val:
                line += f" — {code_val}"
            buckets[kind].append((sort_dt, line))

        def _mk(bucket):
            if not bucket:
                return "—"
            bucket.sort(key=lambda x: x[0])
            return "\n".join([x[1] for x in bucket[:6]])

        purchased_ofd = _mk(buckets["ofd"])
        purchased_service = _mk(buckets["service"])
        purchased_edo = _mk(buckets["edo"])
    except Exception as e:
        logger.error("codes error: %s", e)

    # Проверка на «наш клиент»
    looks_like_our = await maybe_our_client(inn or None, phone or None)
    extra_done = bool(ctx.user_data.get("extra_verify_done"))
    show_finish_verify = (not verified) and looks_like_our and not extra_done

    # блок лояльности
    loyalty_block = ""
    if verified:
        try:
            from services.loyalty_engine import is_loyalty_enabled, get_user_loyalty_summary
            from services.bot_loyalty_ui import format_loyalty_block
            if await is_loyalty_enabled():
                summary = await get_user_loyalty_summary(db_id)
                loyalty_block = "\n\nПрограмма лояльности\n" + format_loyalty_block(summary, html=False)
        except Exception:
            pass

    # Текст профиля
    text = (
        f"{(first_name + ' ' + last_name).strip() or '—'}\n\n"
        f"Телефон: {phone or '—'}\n"
        f"Email: {email or '—'}\n"
        f"ИНН: {inn or '—'}\n"
        f"Город: {city or '—'}\n\n"
        f"Сроки\n"
        f"{kkt_block if verified else '—'}"
        f"\n\nКупленные коды ОФД\n{purchased_ofd}"
        f"\n\nКупленные коды сервисов\n{purchased_service}"
        f"\n\nКупленные коды ЭДО\n{purchased_edo}"
        f"{loyalty_block}"
    )

    # Клавиатура профиля
    rows = []
    if verified:
        rows.append([InlineButton("Реферальный QR", callback_data="my_referral_qr")])
        rows.append([InlineButton("⭐ История баллов", callback_data="loyalty_history")])
        rows.append([InlineButton("🎯 Реферальный прогресс", callback_data="referral_progress")])
    rows.append([InlineButton("🧾 Мои заказы", callback_data="my_orders")])
    rows.append([InlineButton("📦 Мои покупки", callback_data="my_codes")])
    rows.append([InlineButton("✏️ Редактировать профиль", callback_data="edit_profile")])
    if (not verified) and show_finish_verify:
        rows.append([InlineButton("🔐 Пройти доп. верификацию", callback_data="finish_verify")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, text, keyboard=kb, message_id=mid)
    _set_state(ctx, ST_IDLE)


# ══════════════════════════════════════════════════════════════════════
# Лояльность
# ══════════════════════════════════════════════════════════════════════

async def handle_loyalty_history(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «История баллов»."""
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    back_kb = InlineKeyboard(rows=[[InlineButton("⬅️ В профиль", callback_data="profile")]])

    if not await _require_verified_for_loyalty(ctx, chat_id, message_id=mid):
        return

    text = "Программа лояльности отключена."
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
        from services.loyalty_engine import is_loyalty_enabled, get_user_loyalty_summary
        from services.bot_loyalty_ui import format_loyalty_history, format_loyalty_block
        if await is_loyalty_enabled():
            summary = await get_user_loyalty_summary(db_id)
            header = format_loyalty_block(summary, html=False)
            history = format_loyalty_history(summary.get("history", []), html=False)
            text = f"Программа лояльности\n{header}\n\n{history}"
    except Exception:
        text = "Не удалось загрузить историю. Попробуйте позже."

    await _send_or_edit(ctx, chat_id, text, keyboard=back_kb, message_id=mid)


async def handle_referral_progress(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «Реферальный прогресс»."""
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    back_kb = InlineKeyboard(rows=[[InlineButton("⬅️ В профиль", callback_data="profile")]])

    if not await _require_verified_for_loyalty(ctx, chat_id, message_id=mid):
        return

    text = "Программа лояльности отключена."
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
        from services.loyalty_engine import is_loyalty_enabled
        from services.referral_engine import get_referral_progress
        from services.bot_loyalty_ui import format_referral_progress
        if await is_loyalty_enabled():
            progress = await get_referral_progress(db_id)
            text = format_referral_progress(progress, html=False)
    except Exception:
        text = "Не удалось загрузить прогресс. Попробуйте позже."

    await _send_or_edit(ctx, chat_id, text, keyboard=back_kb, message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# Сроки ОФД (отдельная кнопка)
# ══════════════════════════════════════════════════════════════════════

async def show_profile_ofd(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «Сроки ОФД» — отдельный экран с датами истечения ОФД по ККТ."""
    if not cb.user:
        return

    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception:
        await _send_text(ctx, chat_id, "⚠️ Временная ошибка. Повторите позже.")
        return

    inn = ctx.user_data.get("inn") or await get_user_inn_by_id(db_id)

    back_kb = InlineKeyboard(rows=[[InlineButton("⬅️ В профиль", callback_data="profile")]])

    if not inn:
        await _send_or_edit(
            ctx, chat_id,
            "Чтобы показать сроки ОФД, укажите ИНН.",
            keyboard=InlineKeyboard(rows=[
                [InlineButton("✏ Ввести ИНН", callback_data="edit_inn")],
                [InlineButton("⬅️ В профиль", callback_data="profile")],
            ]),
            message_id=mid,
        )
        return

    client = await get_client_by_inn(inn)
    if not client:
        await _send_or_edit(
            ctx, chat_id,
            f"По ИНН {inn} в нашей базе пока нет данных.",
            keyboard=back_kb, message_id=mid,
        )
        return

    slots = await get_client_kkt_slots(client["id"]) or []
    if not slots:
        await _send_or_edit(
            ctx, chat_id,
            f"По ИНН {inn} слоты ККТ не найдены.",
            keyboard=back_kb, message_id=mid,
        )
        return

    today = datetime.date.today()
    lines = [f"ИНН: {inn}", ""]
    for s in slots:
        ofd_date = s.get("ofd_expires_at")
        slot_name = s.get("kkt_slot") or "тт.1"
        if not ofd_date:
            lines.append(f"• {slot_name}: нет данных")
            continue
        lines.append(f"• {slot_name} — {_fmt_date(ofd_date, today)}")
    lines.append("")
    lines.append("Если данные отличаются от фактических — напишите нам, актуализируем.")

    await _send_or_edit(ctx, chat_id, "\n".join(lines), keyboard=back_kb, message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# Мои покупки
# ══════════════════════════════════════════════════════════════════════

async def handle_my_codes(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Экран «Мои покупки» с пагинацией."""
    if not cb.user:
        return

    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception as e:
        logger.exception("accept_consent resolve_db_id failed: %s", e)
        ctx.user_data.setdefault("_max_uid", max_uid)
        await _send_text(ctx, chat_id, "⚠️ Временная ошибка подключения к базе. Проверьте настройки БД и повторите.")
        return
    mid = cb.message.message_id if cb.message else None

    payload = cb.payload or "my_codes"
    offset = 0
    if ":" in payload:
        try:
            offset = max(0, int(payload.split(":", 1)[1]))
        except Exception:
            offset = 0

    rows = await get_user_purchases_by_internal_id(internal_user_id=db_id, limit=PAGE_SIZE_CODES + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE_CODES
    rows = rows[:PAGE_SIZE_CODES]
    has_prev = offset > 0

    if not rows:
        kb = _profile_back_kb()
        await _send_or_edit(ctx, chat_id,
                            "Пока покупок нет. После оплаты здесь появятся данные о заказах.",
                            keyboard=kb, message_id=mid)
        return

    lines = []
    for i, r in enumerate(rows, start=offset + 1):
        item_type = (r.get("item_type") or "").strip().lower()
        title = (r.get("item_title") or "").strip() or ("Оборудование" if item_type == "equip" else "Покупка")
        code = (r.get("code") or "").strip()
        oid = r.get("order_id") or ""
        amt = int(r.get("amount") or 0)
        issued = r.get("issued_at") or ""

        exp_s = ""
        exp = r.get("expires_at")
        if exp:
            try:
                exp_dt = exp
                if isinstance(exp, str):
                    exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", ""))
                exp_s = f" — до {exp_dt:%d.%m.%Y}"
            except Exception:
                pass

        if item_type == "equip":
            lines.append(f"{i}. {title} — заказ {oid} — {amt} ₽ — {issued}")
        else:
            lines.append(f"{i}. {title}: {code}{exp_s} — заказ {oid} — {amt} ₽ — {issued}")

    text = "📦 Мои покупки\n\n" + "\n".join(lines)

    # Пагинация
    next_off = offset + PAGE_SIZE_CODES
    prev_off = max(0, offset - PAGE_SIZE_CODES)
    btns = []
    if has_prev:
        btns.append(InlineButton("⬅️ Назад", callback_data=f"codes_prev:{prev_off}"))
    if has_next:
        btns.append(InlineButton("➡️ Далее", callback_data=f"codes_next:{next_off}"))

    kb_rows = []
    if btns:
        kb_rows.append(btns)
    kb_rows.append([InlineButton("⬅️ В профиль", callback_data="profile")])

    await _send_or_edit(ctx, chat_id, text, keyboard=InlineKeyboard(rows=kb_rows), message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# Редактирование профиля
# ══════════════════════════════════════════════════════════════════════

async def handle_edit_profile(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Меню редактирования профиля."""
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception as e:
        logger.exception("accept_consent resolve_db_id failed: %s", e)
        ctx.user_data.setdefault("_max_uid", max_uid)
        await _send_text(ctx, chat_id, "⚠️ Временная ошибка подключения к базе. Проверьте настройки БД и повторите.")
        return
    mid = cb.message.message_id if cb.message else None

    # Проверяем согласие
    if not (await _has_consent_by_id(db_id)):
        await _show_consent(ctx, chat_id, message_id=mid)
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("Имя", callback_data="edit_firstname"),
         InlineButton("Фамилия", callback_data="edit_lastname")],
        [InlineButton("Телефон", callback_data="edit_phone"),
         InlineButton("Email", callback_data="edit_email")],
        [InlineButton("ИНН", callback_data="edit_inn"),
         InlineButton("Город", callback_data="edit_city")],
        [InlineButton("⬅️ Назад в профиль", callback_data="profile")],
    ])
    await _send_or_edit(ctx, chat_id, "Что изменить?", keyboard=kb, message_id=mid)
    _set_state(ctx, ST_IDLE)


# ── Промпты редактирования ────────────────────────────────────────────

async def handle_edit_firstname_prompt(ctx: MaxBotContext, cb: MaxCallback) -> None:
    mid = cb.message.message_id if cb.message else None
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    await _send_or_edit(ctx, chat_id, "Введите новое имя:", message_id=mid)
    _set_state(ctx, ST_EDIT_FIRSTNAME)


async def handle_edit_lastname_prompt(ctx: MaxBotContext, cb: MaxCallback) -> None:
    mid = cb.message.message_id if cb.message else None
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    await _send_or_edit(ctx, chat_id, "Введите новую фамилию:", message_id=mid)
    _set_state(ctx, ST_EDIT_LASTNAME)


async def handle_edit_phone_prompt(ctx: MaxBotContext, cb: MaxCallback) -> None:
    mid = cb.message.message_id if cb.message else None
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    await _send_or_edit(ctx, chat_id,
                        "Введите новый номер телефона (например: +79001234567):",
                        message_id=mid)
    _set_state(ctx, ST_EDIT_PHONE)


async def handle_edit_email_prompt(ctx: MaxBotContext, cb: MaxCallback) -> None:
    mid = cb.message.message_id if cb.message else None
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    await _send_or_edit(ctx, chat_id, "Введите новый e-mail:", message_id=mid)
    _set_state(ctx, ST_EDIT_EMAIL)


async def handle_edit_inn_prompt(ctx: MaxBotContext, cb: MaxCallback) -> None:
    mid = cb.message.message_id if cb.message else None
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    kb = _profile_back_kb()
    await _send_or_edit(ctx, chat_id, "Введите ИНН (10 или 12 цифр):", keyboard=kb, message_id=mid)
    _set_state(ctx, ST_EDIT_INN)


async def handle_edit_city_prompt(ctx: MaxBotContext, cb: MaxCallback) -> None:
    mid = cb.message.message_id if cb.message else None
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    await _send_or_edit(ctx, chat_id, "Введите новый город:", message_id=mid)
    _set_state(ctx, ST_EDIT_CITY)


# ── Loyalty: бонус за заполнение поля профиля ─────────────────────────

async def _try_award_profile_bonus(internal_user_id: int, field_name: str) -> None:
    """Best-effort начисление баллов за заполнение поля профиля."""
    try:
        from services.loyalty_engine import is_loyalty_enabled, award_profile_completion_bonus

        if not await is_loyalty_enabled():
            return
        await award_profile_completion_bonus(internal_user_id, field_name=field_name)
    except Exception:
        logger.debug(
            "Profile bonus failed for user=%s field=%s — не критично",
            internal_user_id, field_name, exc_info=True,
        )


# ── Сохранение полей ──────────────────────────────────────────────────

async def _save_firstname(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    val = (msg.text or "").strip()
    if not val:
        await _send_text(ctx, msg.chat_id, "Введите имя:")
        return True
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, first_name=val)
    except Exception as e:
        logger.exception("update first_name failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось обновить имя. Попробуйте ещё раз.")
        return True
    ctx.user_data["first_name"] = val
    await _try_award_profile_bonus(db_id, "first_name")
    _set_state(ctx, ST_IDLE)
    await _send_text(ctx, msg.chat_id, "✅ Имя обновлено.", keyboard=_profile_back_kb())
    return True


async def _save_lastname(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    val = (msg.text or "").strip()
    if not val:
        await _send_text(ctx, msg.chat_id, "Введите фамилию:")
        return True
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, last_name=val)
    except Exception as e:
        logger.exception("update last_name failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось обновить фамилию. Попробуйте ещё раз.")
        return True
    ctx.user_data["last_name"] = val
    await _try_award_profile_bonus(db_id, "last_name")
    _set_state(ctx, ST_IDLE)
    await _send_text(ctx, msg.chat_id, "✅ Фамилия обновлена.", keyboard=_profile_back_kb())
    return True


async def _save_phone(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    text = (msg.text or "").strip()
    phone = _normalize_phone(text)
    if not phone:
        await _send_text(ctx, msg.chat_id, "Не удалось распознать номер. Введите в формате +79001234567:")
        return True
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, phone=phone)
    except Exception as e:
        logger.exception("save phone failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось сохранить номер. Повторите ввод.")
        return True

    ctx.user_data["phone"] = phone
    ctx.user_data["phone_confirmed"] = True
    await _try_award_profile_bonus(db_id, "phone")
    _set_state(ctx, ST_IDLE)

    await _send_text(ctx, msg.chat_id, f"✅ Телефон обновлён: {phone}", keyboard=_profile_back_kb())

    # Если есть ИНН — пробуем верифицировать
    if ctx.user_data.get("inn"):
        await _verify_and_finish(ctx, msg.chat_id)
    return True


async def _save_email(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    email = (msg.text or "").strip()
    if not EMAIL_RE.match(email):
        await _send_text(ctx, msg.chat_id, "❌ Некорректный e-mail. Пример: user@example.com")
        return True
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, email=email)
    except Exception as e:
        logger.exception("update email failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось обновить e-mail. Попробуйте ещё раз.")
        return True
    ctx.user_data["email"] = email
    await _try_award_profile_bonus(db_id, "email")
    _set_state(ctx, ST_IDLE)
    await _send_text(ctx, msg.chat_id, "✅ E-mail обновлён.", keyboard=_profile_back_kb())
    return True


async def _save_inn(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    raw_digits = "".join(ch for ch in (msg.text or "") if ch.isdigit())
    if len(raw_digits) not in (10, 12):
        await _send_text(
            ctx, msg.chat_id,
            "❌ Неверная длина ИНН.\n"
            "Введите 10 цифр (для ООО/ОАО) или 12 цифр (для ИП и физлица).\n\n"
            "Например: 7707083893 или 500100732259",
        )
        return True
    if not _validate_inn_checksum(raw_digits):
        await _send_text(
            ctx, msg.chat_id,
            "❌ ИНН не прошёл проверку контрольного числа.\n"
            "Убедитесь, что вы ввели его без ошибок.",
        )
        return True
    inn = raw_digits
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, inn=inn)
    except Exception as e:
        logger.exception("update inn failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось обновить ИНН. Попробуйте ещё раз.")
        return True
    ctx.user_data["inn"] = inn
    ctx.user_data.pop("skipped_inn", None)
    await _try_award_profile_bonus(db_id, "inn")

    # Check if we came from checkout
    mode = ctx.user_data.pop("co_mode", None)
    if mode == "checkout":
        _set_state(ctx, ST_IDLE)
        await _send_text(ctx, msg.chat_id, "✅ ИНН сохранён.")
        from max_catalog import checkout
        await checkout(ctx, msg.chat_id)
        return True

    _set_state(ctx, ST_IDLE)
    await _send_text(ctx, msg.chat_id, "✅ ИНН сохранён.", keyboard=_profile_back_kb())

    if ctx.user_data.get("phone_confirmed"):
        await _verify_and_finish(ctx, msg.chat_id)
    return True


async def _save_city(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    city = (msg.text or "").strip()
    if not city:
        await _send_text(ctx, msg.chat_id, "Укажите город:")
        return True
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        await update_user_by_internal_id(db_id, city=city)
    except Exception as e:
        logger.exception("update city failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось обновить город. Попробуйте ещё раз.")
        return True
    ctx.user_data["city"] = city
    await _try_award_profile_bonus(db_id, "city")

    # Check if we came from checkout
    if ctx.user_data.pop("co_mode", None) == "checkout":
        _set_state(ctx, ST_IDLE)
        from max_catalog import checkout
        await checkout(ctx, msg.chat_id)
        return True

    _set_state(ctx, ST_IDLE)
    await _send_text(ctx, msg.chat_id, "✅ Город обновлён.", keyboard=_profile_back_kb())
    return True


async def _save_kkt_factory_number(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Save KKT factory number and return to checkout if needed."""
    kkt_number = (msg.text or "").strip()
    if not re.fullmatch(r"\d+", kkt_number):
        await _send_text(ctx, msg.chat_id, "❌ Некорректный номер. Введите ещё раз")
        return True
    
    try:
        db_id = await _ensure_db_id_from_sender(ctx, str(msg.sender.user_id))
        # Update KKT factory number in database
        from db import set_user_kkt_factory_number_by_id
        await set_user_kkt_factory_number_by_id(db_id, kkt_number)
    except Exception as e:
        logger.exception("save kkt_factory_number failed: %s", e)
        await _send_text(ctx, msg.chat_id, "⚠️ Не удалось сохранить номер ККТ. Попробуйте ещё раз.")
        return True
    
    ctx.user_data["kkt_factory_number"] = kkt_number
    
    # Check if we came from checkout
    mode = ctx.user_data.pop("co_mode", None)
    if mode == "checkout":
        _set_state(ctx, ST_IDLE)
        await _send_text(ctx, msg.chat_id, "✅ Заводской номер ККТ сохранён.")
        from max_catalog import checkout
        await checkout(ctx, msg.chat_id)
        return True
    
    _set_state(ctx, ST_IDLE)
    await _send_text(ctx, msg.chat_id, "✅ Заводской номер ККТ сохранён.", keyboard=_profile_back_kb())
    return True


# ══════════════════════════════════════════════════════════════════════
# Checkout flow handlers
# ══════════════════════════════════════════════════════════════════════

async def handle_input_new_inn(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback handler for requesting INN during checkout."""
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    ctx.user_data["co_mode"] = "checkout"
    _set_state(ctx, ST_CO_INN)
    
    await _send_or_edit(
        ctx,
        chat_id,
        "Введите ИНН (10 или 12 цифр):",
        message_id=mid
    )


async def handle_input_new_kkt_number(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback handler for requesting KKT factory number during checkout."""
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    ctx.user_data["co_mode"] = "checkout"
    _set_state(ctx, ST_CO_KKT_FACTORY_NUMBER)
    
    await _send_or_edit(
        ctx,
        chat_id,
        "Введите заводской номер ККТ:",
        message_id=mid
    )


# ══════════════════════════════════════════════════════════════════════
# Главное меню (динамическое)
# ══════════════════════════════════════════════════════════════════════

async def _show_main_menu(ctx: MaxBotContext, chat_id: str, title: str = "Чем могу помочь?",
                           message_id: Optional[str] = None,
                           user_id: Optional[int] = None) -> None:
    """Показать главное меню с учётом состояния пользователя.

    Логика:
    - Пробуем отредактировать message_id (текущее сообщение).
    - Если не получилось — отправляем новое.
    - Удаляем старое меню (если оно отличается от нового).
    - Очищаем накопленные PII-сообщения из регистрации.
    """
    kb = await _build_main_menu_kb(ctx)

    old_menu_mid = ctx.user_data.get("_menu_msg_id")

    result = await _send_or_edit(ctx, chat_id, title, keyboard=kb, message_id=message_id, user_id=user_id)

    used_mid = result or message_id

    if used_mid:
        ctx.user_data["_menu_msg_id"] = used_mid

    # Удаляем старое меню, если оно отличается от нового
    if old_menu_mid and old_menu_mid != used_mid:
        asyncio.create_task(_safe_delete(ctx.api, old_menu_mid))

    # Также чистим message_id (сообщение с кнопкой, по которой нажали),
    # если оно отличается от итогового
    if message_id and message_id != used_mid:
        asyncio.create_task(_safe_delete(ctx.api, message_id))

    # Чистим PII-сообщения (телефон, ИНН) из регистрации
    keep = set()
    if used_mid:
        keep.add(used_mid)
    await reg_cleanup_messages(ctx, keep_ids=keep)

    _set_state(ctx, ST_IDLE)


async def _build_main_menu_kb(ctx: MaxBotContext) -> InlineKeyboard:
    """Строит главное меню с учётом прав пользователя."""
    consent_ok = ctx.user_data.get("_consent_ok", False)
    verified = bool(ctx.user_data.get("_verified", False))
    # Фикс: при возврате из некоторых разделов (например, из магазина)
    # в памяти может не быть актуальных _db_id/_verified.
    # Перед сборкой главного меню дотягиваем идентификатор и признак верификации из БД (best effort).
    db_id = int(ctx.user_data.get("_db_id") or 0)
    if not db_id:
        max_uid = str(ctx.user_data.get("_max_uid") or "").strip()
        if max_uid:
            try:
                db_id = int(await _resolve_db_id(ctx, max_uid))
            except Exception:
                db_id = 0
    if db_id and not verified:
        try:
            verified = bool(await is_user_verified_by_internal_id(db_id))
            ctx.user_data["_verified"] = verified
        except Exception:
            pass
    phone_confirmed = ctx.user_data.get("phone_confirmed", False)
    inn_ok = bool((ctx.user_data.get("inn") or "").strip())

    agent_ctx_check = ctx.user_data.get("agent_ctx")
    is_spec_max = isinstance(agent_ctx_check, dict) and agent_ctx_check.get("role") in ("specialist", "director")

    rows = []

    if not is_spec_max:
        rows.append([InlineButton("🔗 Реферальный QR (получи скидку)", callback_data="my_referral_qr")])
        rows.append([InlineButton("💬 Мои обращения", callback_data="my_chats")])
    rows.append([InlineButton("🛠 Тех. поддержка", callback_data="client_chat_start:1")])
    rows.append([InlineButton("🧰 Услуги", callback_data="services")])
    rows.append([InlineButton("🧾 Бухгалтерия", callback_data="client_chat_start:0")])

    # Баланс баллов — только для верифицированных пользователей
    _loyalty_badge = ""
    if verified:
        try:
            from services.loyalty_engine import is_loyalty_enabled
            from services.ledger import get_available_balance
            if await is_loyalty_enabled():
                _db_id = ctx.user_data.get("_db_id")
                if _db_id:
                    _bal = await get_available_balance("client", _db_id)
                    if _bal and _bal > 0:
                        _loyalty_badge = f" | ⭐ {_bal}"
        except Exception:
            pass
    rows.append([InlineButton(f"👤 Мой профиль{_loyalty_badge}", callback_data="profile")])
    if not is_spec_max:
        rows.append([
            InlineButton("🇷🇺 Магазин уЮрты", callback_data="buy"),
            InlineButton("🛒 Корзина", callback_data="cart"),
        ])

    # Директор: отдельные кнопки "Мои чаты (специалист)" и "Заказы"
    agent_ctx = ctx.user_data.get("agent_ctx")
    if isinstance(agent_ctx, dict) and agent_ctx.get("role") == "director":
        rows.append([InlineButton("🧑‍💼 Мои чаты (специалист)", callback_data="ag_dir_sections")])
        rows.append([InlineButton("📋 Заказы", callback_data="users_with_orders")])

    # Кнопка «Закончить верификацию» если телефон/ИНН не заполнен
    if consent_ok and not verified:
        need_finish = (not phone_confirmed) or (not inn_ok)
        if need_finish:
            rows.append([InlineButton("🔐 Закончить верификацию", callback_data="finish_verify")])

    # Кнопка «Доп. верификация» (если verified, но extra_verify не сделана)
    extra_done = bool(ctx.user_data.get("extra_verify_done"))
    extra_offer = bool(ctx.user_data.get("extra_verify_offer"))
    if verified and not extra_done and extra_offer:
        rows.append([InlineButton("🔍 Доп. верификация", callback_data="extra_verify")])

    # Кнопка «Админка» для администраторов MAX
    from max_catalog import _is_max_admin
    max_uid = ctx.user_data.get("_max_uid")
    if max_uid:
        try:
            if _is_max_admin(int(max_uid)):
                rows.append([InlineButton("🛠 Админка", callback_data="admin_menu")])
        except (ValueError, TypeError):
            pass

    return InlineKeyboard(rows=rows)


# ══════════════════════════════════════════════════════════════════════
# /wipe_me — сброс профиля
# ══════════════════════════════════════════════════════════════════════

async def handle_wipe_me(ctx: MaxBotContext, msg: MaxMessage) -> None:
    """Команда /wipe_me — полный сброс профиля, перезапуск регистрации."""
    if not msg.sender:
        return

    max_uid = str(msg.sender.user_id)
    db_id = await _resolve_db_id(ctx, max_uid)

    try:
        await reset_user_profile_by_internal_id(db_id)
    except Exception as e:
        logger.error("reset_user_profile_by_internal_id error: %s", e)

    try:
        await log_event(event_type="command", internal_user_id=db_id, name="/wipe_me",
                        payload={"source": "max"})
    except Exception:
        pass

    # Очищаем локальный кэш
    ctx.user_data.clear()
    ctx.user_data["_db_id"] = db_id
    ctx.user_data["_max_uid"] = max_uid

    kb = InlineKeyboard(rows=[
        [InlineButton("📝 Начать регистрацию", callback_data="register")],
    ])
    await ctx.api.send_message(
        msg.chat_id,
        "Профиль очищен. Нажмите «Начать регистрацию» для повторной настройки.",
        keyboard=kb,
    )


# ══════════════════════════════════════════════════════════════════════
# Application Form (Заявка / Оформление)
# ══════════════════════════════════════════════════════════════════════

async def handle_application_form_start(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Entry point for application form — check consent + phone."""
    if not cb.user:
        return
    
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    
    try:
        db_id = await _resolve_db_id(ctx, max_uid)
    except Exception as e:
        logger.exception("application_form_start resolve_db_id failed: %s", e)
        await _send_text(ctx, chat_id, "⚠️ Временная ошибка подключения к базе.")
        return
    
    await _hydrate_user_cache(ctx, db_id)
    
    # 1) Check consent
    if not (await _has_consent_by_id(db_id)):
        await _start_registration(ctx, chat_id)
        return
    
    # 2) Check confirmed phone
    if not ctx.user_data.get("phone_confirmed"):
        await _send_text(
            ctx,
            chat_id,
            "Для отправки заявки нам нужен подтверждённый номер телефона.\n\n"
            "Пожалуйста, поделитесь номером через MAX:",
        )
        phone_kb = InlineKeyboard(rows=[
            [InlineButton("📱 Подтвердить номер из MAX", request_contact=True)],
        ])
        await _send_text(ctx, chat_id, "Нажмите кнопку ниже:", keyboard=phone_kb)
        _set_state(ctx, ST_WAIT_PHONE)
        return
    
    # 3) Start form
    ctx.user_data["form"] = {}
    await _send_or_edit(
        ctx,
        chat_id,
        "📝 Анкета клиента\n\nВведите Название торговой точки:",
        keyboard=_back_kb(),
    )
    _set_state(ctx, ST_FORM_TP)


async def _form_tp(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Save trade point name and move to address."""
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["tp_name"] = (msg.text or "").strip()
    
    await asyncio.sleep(1)
    await _send_text(ctx, msg.chat_id, "Адрес торговой точки:", keyboard=_back_kb())
    _set_state(ctx, ST_FORM_ADDR)
    return True


async def _form_addr(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Save address and show activity buttons."""
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["tp_addr"] = (msg.text or "").strip()
    
    kb = InlineKeyboard(rows=[
        [InlineButton("Продовольственная розница", callback_data="act_food")],
        [InlineButton("Непродовольственная розница", callback_data="act_nonfood")],
        [InlineButton("Услуги", callback_data="act_services")],
        [InlineButton("Оптовая торговля", callback_data="act_wholesale")],
        [InlineButton("Маркетплейсы", callback_data="act_marketplaces")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    
    await asyncio.sleep(1)
    await _send_text(ctx, msg.chat_id, "Выберите вид деятельности:", keyboard=kb)
    _set_state(ctx, ST_FORM_ACT)
    return True


async def handle_form_act_cb(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Handle activity selection."""
    if not cb.user:
        return
    
    act_map = {
        "act_food": "продовольственная розница",
        "act_nonfood": "непродовольственная розница",
        "act_services": "услуги",
        "act_wholesale": "оптовая торговля",
        "act_marketplaces": "маркетплейсы",
    }
    
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["activity"] = act_map.get(cb.payload, "")
    
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    await _send_or_edit(
        ctx,
        chat_id,
        "Опишите основу ассортимента:",
        keyboard=_back_kb(),
        message_id=mid,
    )
    _set_state(ctx, ST_FORM_ASSORT)


async def _form_assort(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Save assortment and show services multi-select."""
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["assort"] = (msg.text or "").strip()
    ctx.user_data["form"]["services"] = ctx.user_data["form"].get("services") or []
    
    kb = _services_kb(ctx.user_data["form"]["services"])
    
    await asyncio.sleep(1)
    await _send_text(
        ctx,
        msg.chat_id,
        "Выберите дополнительные сервисы (можно несколько):\n"
        "Нажимайте по пунктам, затем «Готово».",
        keyboard=kb,
    )
    _set_state(ctx, ST_FORM_SERVICES)
    return True


def _services_kb(selected: list[str] | None = None) -> InlineKeyboard:
    """Keyboard for services multi-select."""
    selected = selected or []
    options = [
        ("Честный знак", "svc_chz"),
        ("Меркурий", "svc_merk"),
        ("ЕГАИС", "svc_ega"),
        ("Статистика", "svc_stat"),
    ]
    
    rows = []
    for label, cb_data in options:
        prefix = "✅ " if label in selected else ""
        rows.append([InlineButton(prefix + label, callback_data=cb_data)])
    
    rows.append([InlineButton("Готово", callback_data="svc_done")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])
    return InlineKeyboard(rows=rows)


async def handle_form_services_cb(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Handle services selection (multi-select)."""
    if not cb.user:
        return
    
    data = cb.payload
    form = ctx.user_data.setdefault("form", {})
    services = form.get("services") or []
    
    label_map = {
        "svc_chz": "Честный знак",
        "svc_merk": "Меркурий",
        "svc_ega": "ЕГАИС",
        "svc_stat": "Статистика",
    }
    
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    # If "Done" — move to SNO
    if data == "svc_done":
        kb = InlineKeyboard(rows=[
            [InlineButton("УСН 6%", callback_data="sno_usn6")],
            [InlineButton("УСН Д-Р 15%", callback_data="sno_usn15")],
            [InlineButton("Патент", callback_data="sno_patent")],
            [InlineButton("ОСНО", callback_data="sno_osn")],
            [InlineButton("Самозанятый", callback_data="sno_self")],
            [InlineButton("🔙 В меню", callback_data="back_to_main")],
        ])
        await _send_or_edit(
            ctx,
            chat_id,
            "Укажите систему налогообложения (СНО):",
            keyboard=kb,
            message_id=mid,
        )
        _set_state(ctx, ST_FORM_SNO)
        return
    
    # Toggle service
    label = label_map.get(data)
    if not label:
        return
    
    if label in services:
        services.remove(label)
    else:
        services.append(label)
    
    form["services"] = services
    
    kb = _services_kb(services)
    chosen_text = ", ".join(services) if services else "ничего не выбрано"
    await _send_or_edit(
        ctx,
        chat_id,
        "Выберите дополнительные сервисы (можно несколько):\n"
        f"Сейчас отмечено: {chosen_text}",
        keyboard=kb,
        message_id=mid,
    )


async def handle_form_sno_cb(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Handle SNO selection."""
    if not cb.user:
        return
    
    sno_map = {
        "sno_usn6": "УСН 6%",
        "sno_usn15": "УСН Д-Р 15%",
        "sno_patent": "Патент",
        "sno_osn": "ОСНО",
        "sno_self": "Самозанятый",
    }
    
    form = ctx.user_data.setdefault("form", {})
    form["sno"] = sno_map.get(cb.payload, "")
    
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    kb = InlineKeyboard(rows=[
        [InlineButton("Да", callback_data="marked_yes"),
         InlineButton("Нет", callback_data="marked_no")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(
        ctx,
        chat_id,
        "Продаёте маркированный товар?",
        keyboard=kb,
        message_id=mid,
    )
    _set_state(ctx, ST_FORM_MARKED)


async def handle_form_marked_cb(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Handle marked goods yes/no."""
    if not cb.user:
        return
    
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["marked"] = "да" if cb.payload.endswith("yes") else "нет"
    
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    kb = InlineKeyboard(rows=[
        [InlineButton("ФН 15 мес", callback_data="fn_15_term"),
         InlineButton("ФН 36 мес", callback_data="fn_36_term")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, "Срок действия ФН:", keyboard=kb, message_id=mid)
    _set_state(ctx, ST_FORM_FNTERM)


async def handle_form_fnterm_cb(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Handle FN term selection."""
    if not cb.user:
        return
    
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["fn_term"] = "15 мес" if "15" in cb.payload else "36 мес"
    
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    kb = InlineKeyboard(rows=[
        [InlineButton("Любой", callback_data="ofd_any")],
        [InlineButton("Первый ОФД", callback_data="ofd_first_opt")],
        [InlineButton("Платформа ОФД", callback_data="ofd_platform_opt")],
    ])
    await _send_or_edit(ctx, chat_id, "ОФД:", keyboard=kb, message_id=mid)
    _set_state(ctx, ST_FORM_OFD)


async def handle_form_ofd_cb(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Handle OFD selection."""
    if not cb.user:
        return
    
    m = cb.payload
    ofd = "Любой" if "any" in m else ("Первый ОФД" if "first" in m else "Платформа ОФД")
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["ofd"] = ofd
    
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    
    await _send_or_edit(
        ctx,
        chat_id,
        "В каких банках у вас есть открытый расчётный счёт?",
        keyboard=_back_kb(),
        message_id=mid,
    )
    _set_state(ctx, ST_FORM_BANKS)


async def _form_banks(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Final step — save banks and submit to Django CRM."""
    ctx.user_data.setdefault("form", {})
    ctx.user_data["form"]["banks"] = (msg.text or "").strip()
    
    f = ctx.user_data["form"]
    
    # Profile data for CRM contact
    name = ctx.user_data.get("first_name") or "Клиент"
    phone = (ctx.user_data.get("phone") or "").strip()
    email = (ctx.user_data.get("email") or "").strip()
    inn = (ctx.user_data.get("inn") or "").strip()
    city = (ctx.user_data.get("city") or "").strip()
    max_uid = ctx.user_data.get("_max_uid", "")
    
    # Build detailed note
    note_lines = [
        "Заявка/Оформление (MAX)",
        "====================",
        "Профиль клиента:",
        f"• Имя: {name or '—'}",
        f"• Телефон: {phone or '—'}",
        f"• E-mail: {email or '—'}",
        f"• ИНН: {inn or '—'}",
        f"• Город: {city or '—'}",
        f"• MAX user_id: {max_uid}",
        "",
        "Анкета:",
        f"• Название ТТ: {f.get('tp_name') or '—'}",
        f"• Адрес ТТ: {f.get('tp_addr') or '—'}",
        f"• Вид деятельности: {f.get('activity') or '—'}",
        f"• Основа ассортимента: {f.get('assort') or '—'}",
        f"• Доп. сервисы: {', '.join(f.get('services', [])) or '—'}",
        f"• СНО: {f.get('sno') or '—'}",
        f"• Маркировка (да/нет): {f.get('marked') or '—'}",
        f"• Срок действия ФН: {f.get('fn_term') or '—'}",
        f"• ОФД: {f.get('ofd') or '—'}",
        f"• Банки с Р/С: {f.get('banks') or '—'}",
    ]
    
    await asyncio.sleep(1)
    
    # Submit to Django CRM (используем общую функцию из django_crm.py)
    try:
        from django_crm import push_application_to_crm
        user_data = {
            "first_name": ctx.user_data.get("first_name", ""),
            "last_name": ctx.user_data.get("last_name", ""),
            "phone": ctx.user_data.get("phone", ""),
            "email": ctx.user_data.get("email", ""),
            "inn": ctx.user_data.get("inn", ""),
            "city": ctx.user_data.get("city", ""),
            "max_user_id": ctx.user_data.get("_max_uid", ""),
        }
        await push_application_to_crm(f, source="max", user_data=user_data)
        await _send_text(ctx, msg.chat_id, "✅ Заявка отправлена. Спасибо! Мы свяжемся с вами.")
    except Exception as e:
        logger.exception("Failed to push application to Django CRM: %s", e)
        await _send_text(ctx, msg.chat_id, f"✅ Заявка принята, но не удалось отправить в CRM: {e}")
    
    _set_state(ctx, ST_IDLE)
    await _show_main_menu(ctx, msg.chat_id, "Чем ещё помочь?")
    return True




# ══════════════════════════════════════════════════════════════════════
# Регистрация всех обработчиков
# ══════════════════════════════════════════════════════════════════════

def register_registration_handlers(bot) -> None:
    """Подключить все обработчики регистрации к MaxBot."""
    from max_bot_main import MaxBot

    # Команды
    bot.register_command("/start", handle_start)
    bot.register_command("/wipe_me", handle_wipe_me)
    bot.register_command("/delete_me", handle_delete_me)

    # Callback-и регистрации
    bot.register_callback("check_subscription", handle_check_subscription_max)
    bot.register_callback("accept_consent", handle_accept_consent)
    bot.register_callback("go_guest", handle_go_guest)
    bot.register_callback("skip_inn", handle_skip_inn)
    bot.register_callback("register", handle_register)
    bot.register_callback("finish_verify", handle_register)

    # Профиль
    bot.register_callback("profile", handle_profile)
    bot.register_callback("profile_ofd", show_profile_ofd)
    bot.register_callback("my_codes", handle_my_codes)
    bot.register_callback("codes_prev:*", handle_my_codes)
    bot.register_callback("codes_next:*", handle_my_codes)

    # Лояльность
    bot.register_callback("loyalty_history", handle_loyalty_history)
    bot.register_callback("referral_progress", handle_referral_progress)

    # Редактирование профиля
    bot.register_callback("edit_profile", handle_edit_profile)
    bot.register_callback("edit_firstname", handle_edit_firstname_prompt)
    bot.register_callback("edit_lastname", handle_edit_lastname_prompt)
    bot.register_callback("edit_phone", handle_edit_phone_prompt)
    bot.register_callback("edit_email", handle_edit_email_prompt)
    bot.register_callback("edit_inn", handle_edit_inn_prompt)
    bot.register_callback("edit_city", handle_edit_city_prompt)
    
    # Checkout flow handlers
    bot.register_callback("input_new_inn", handle_input_new_inn)
    bot.register_callback("input_new_kkt_number", handle_input_new_kkt_number)
    
    # Application form handlers
    bot.register_callback("application_form", handle_application_form_start)
    bot.register_callback("act_food", handle_form_act_cb)
    bot.register_callback("act_nonfood", handle_form_act_cb)
    bot.register_callback("act_services", handle_form_act_cb)
    bot.register_callback("act_wholesale", handle_form_act_cb)
    bot.register_callback("act_marketplaces", handle_form_act_cb)
    bot.register_callback("svc_chz", handle_form_services_cb)
    bot.register_callback("svc_merk", handle_form_services_cb)
    bot.register_callback("svc_ega", handle_form_services_cb)
    bot.register_callback("svc_stat", handle_form_services_cb)
    bot.register_callback("svc_done", handle_form_services_cb)
    bot.register_callback("sno_usn6", handle_form_sno_cb)
    bot.register_callback("sno_usn15", handle_form_sno_cb)
    bot.register_callback("sno_patent", handle_form_sno_cb)
    bot.register_callback("sno_osn", handle_form_sno_cb)
    bot.register_callback("sno_self", handle_form_sno_cb)
    bot.register_callback("marked_yes", handle_form_marked_cb)
    bot.register_callback("marked_no", handle_form_marked_cb)
    bot.register_callback("fn_15_term", handle_form_fnterm_cb)
    bot.register_callback("fn_36_term", handle_form_fnterm_cb)
    bot.register_callback("ofd_any", handle_form_ofd_cb)
    bot.register_callback("ofd_first_opt", handle_form_ofd_cb)
    bot.register_callback("ofd_platform_opt", handle_form_ofd_cb)

    logger.info("MAX-бот: обработчики регистрации подключены")
