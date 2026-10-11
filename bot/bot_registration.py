# bot_registration.py
"""
Регистрация, профиль, согласие, анкета, "Мои покупки" и общий UX.

Сюда надо перенести из старого bot.py:
- старт / идентификация / гостевой режим;
- логика согласия на ПДн;
- профиль, редактирование, "Мои покупки";
- анкета "Заявка/Оформление" + Django CRM;
- refresh / back_to_main / text_fallback;
- reset_me / wipe_me;
- все вспомогательные утилиты, которые используются только здесь:
  нормализация телефона, INN, кеш пользователя, send_or_edit и т.д.
"""

import asyncio
import time
import re
from html import escape
from typing import Optional
from qr_utils import generate_qr_file_hashed
from webapp_settings import (
    TG_REF_BOT_USERNAME,
    MAX_BOT_NAME,
    MAX_REF_BOT_NAME,
    MAX_REF_FALLBACK_BOT_NAME,
)
from notice_center import clear_user_notices_tg
from subscription_check import is_subscribed_tg, TG_REQUIRED_CHANNEL

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from telegram.ext import ApplicationHandlerStop

# Django CRM integration
from django_crm import (
    push_verification_to_crm,
    push_application_to_crm,
)

import os
import sys

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
from db import (
    wipe_user_telegram_identity,
    get_user_purchases,
    get_user,
    upsert_user,
    get_user_inn,
    set_user_inn,
    set_user_city,
    set_user_address,
    set_user_consent,
    reset_user_profile,
    wipe_user_all,
    verify_inn_phone,
    try_merge_user_with_preloaded,
    mark_user_verified,
    is_user_verified,
    maybe_our_client,
    get_user_codes,
    get_client_by_inn,
    get_client_ofd_slots,
    get_client_kkt_slots,
    get_company_owner_kkt_slots_by_inn,
    get_kkt_slots_by_client_id,
    get_referral_code,
    apply_referral_code,
    set_user_kkt_factory_number,

    # specialist helpers (for role-based navigation)
    get_specialist_by_tg,
    get_specialist_by_phone,
    link_specialist_tg_user,
    set_specialist_phone_verified,
    is_partner_specialist,
    get_or_create_specialist_ref_code,
    create_user_ref_code,
    get_user_ref_code,

    # partner chat helpers
    ensure_partner_entrypoint_chat,
    resolve_partner_identity_by_phone,

)

from telegram.error import BadRequest

from bot_common import (
    STATE_IDLE,
    STATE_CONSENT,
    STATE_PRODUCT_SELECT,
    STATE_CART,
    STATE_CO_EMAIL,
    STATE_CO_INN,
    STATE_CO_CITY,
    STATE_CO_ADDRESS,
    STATE_CO_KKT_FACTORY_NUMBER,
    STATE_ADMIN_ADD_CODES,
    STATE_EDIT_MENU,
    STATE_EDIT_FIRSTNAME,
    STATE_EDIT_LASTNAME,
    STATE_EDIT_PHONE,
    STATE_EDIT_EMAIL,
    STATE_EDIT_INN,
    STATE_EDIT_CITY,
    S0_START,
    S0_GUEST,
    S1_IDENT,
    S2_WAIT_CONTACT,
    S4_WAIT_INN,
    S5_HOME,
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
    RU_FLAG,
    CONSENT_VERSION,
    kb_request_contact,
    _debounce_cb,
    send_or_edit,
    nav_kb,
    show_main_menu,
    is_admin,
    _has_consent,
    is_specialist_user,
    _ui,  # TelegramUIFlow instance
)

from bot_catalog import show_services, show_cart, checkout
import datetime
import os
from pathlib import Path

import logging

logger = logging.getLogger(__name__)
WELCOME_IMAGE_PATH = Path(os.getenv("WELCOME_IMAGE_PATH", "./welcome.png"))

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PAGE_SIZE_CODES = 10  # или другое нужное тебе число
TG_QR_NAMESPACE = "telegram_referral"
MAX_QR_NAMESPACE = "max_referral"


# ===== Вспомогательные утилиты =====

def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def normalize_phone_loose(s: str) -> str:
    d = _digits(s)
    if not d:
        return ""
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]
    if d.startswith("7") and len(d) == 11:
        return "+" + d
    if len(d) >= 10:
        return "+" + d
    return d


def _normalize_phone(s: str) -> str | None:
    d = _digits(s)
    if len(d) == 10 and d.startswith("9"):
        d = "7" + d
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    if len(d) == 11 and d.startswith("7"):
        return "+" + d
    return None


def _normalize_inn(raw: str) -> str | None:
    s = "".join(ch for ch in (raw or "") if ch.isdigit())
    return s if len(s) in (10, 12) else None


def _looks_like_inn(s: str) -> bool:
    d = _digits(s);
    return len(d) in (10, 12)


def _looks_like_phone(s: str) -> bool:
    d = _digits(s)
    return len(d) >= 10  # контакт всё равно запросим кнопкой


def schedule_reg_message_for_delete(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        time_to_delete: int = 10 * 60,  # 10 минут
) -> None:
    """
    Запланировать удаление сообщения (бота или пользователя), содержащего персональные данные
    или запрос персональных данных.
    """
    jq = getattr(context, "job_queue", None)
    if jq is None:
        return
    try:
        jq.run_once(
            delete_reg_message_job,
            when=time_to_delete,
            data={"chat_id": chat_id, "message_id": message_id},
            name=f"del_reg_{chat_id}_{message_id}",
        )
    except Exception as e:
        logger.debug("Failed to schedule reg message delete for %s/%s: %s", chat_id, message_id, e)


def schedule_pii_delete(context: ContextTypes.DEFAULT_TYPE, message, time_to_delete: int = 10 * 60) -> None:
    """
    Удобный helper: передаём целый Message — он сам поставит его на удаление.
    Используем для:
    - сообщений пользователя с ПДн;
    - сообщений бота с ПДн/запросами ПДн.
    """
    if not message:
        return
    try:
        schedule_reg_message_for_delete(
            context,
            int(message.chat_id),
            int(message.message_id),
            time_to_delete=int(time_to_delete),
        )
    except Exception as e:
        logger.debug("Failed to schedule PII delete: %s", e)

def reg_track_mid(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    """Запоминаем message_id, которые надо удалить после окончания регистрации."""
    if not chat_id or not message_id:
        return
    key = "reg_cleanup_mids"
    arr = context.user_data.get(key)
    if not isinstance(arr, list):
        arr = []
        context.user_data[key] = arr
    # избегаем дублей и разрастания
    mid = int(message_id)
    if mid not in arr:
        arr.append(mid)
    # ограничим хвост на всякий случай
    if len(arr) > 80:
        context.user_data[key] = arr[-80:]


async def reg_cleanup_messages(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    keep_ids: set[int] | None = None,
) -> None:
    """Удаляем всё, что накопили в reg_cleanup_mids, кроме keep_ids."""
    keep_ids = keep_ids or set()
    key = "reg_cleanup_mids"
    arr = context.user_data.get(key)
    if not isinstance(arr, list) or not arr:
        return

    mids = [int(x) for x in arr if x and int(x) not in keep_ids]
    # чистим список сразу, чтобы не было повторных попыток
    context.user_data.pop(key, None)

    for mid in mids:
        try:
            await context.application.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            # best effort: Telegram может не дать удалить некоторые сообщения
            pass


async def delete_reg_message_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: удалить сообщение с персональными данными (бота ИЛИ пользователя)."""
    job = getattr(context, "job", None)
    data = getattr(job, "data", None) if job is not None else None
    if not isinstance(data, dict):
        return
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    if not chat_id or not message_id:
        return
    try:
        await context.application.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.debug("Failed to delete reg message %s in chat %s: %s", message_id, chat_id, e)


async def hydrate_user_cache(context: ContextTypes.DEFAULT_TYPE, uid: str) -> None:
    """
    Подтягивает профиль из БД в context.user_data.

    Важно: из-за bot_state.pkl старые значения могут "залипать", поэтому
    синхронизируем ключевые флаги из БД каждый раз (consent/is_verified/phone).
    """
    try:
        u = await get_user(uid)
        if u:
            for k in ("first_name", "last_name", "phone", "email", "city", "tg_username"):
                if u.get(k) is not None:
                    context.user_data[k] = u.get(k)

            # ИНН
            inn_db = (u.get("inn") or "") if isinstance(u, dict) else ""
            if inn_db:
                context.user_data["inn"] = inn_db

            # флаги
            context.user_data["phone_confirmed"] = bool(u.get("phone"))
            context.user_data["_consent_ok"] = bool(u.get("consent_accepted") or u.get("consent_accepted_at"))
            context.user_data["_verified"] = bool(u.get("is_verified"))

        else:
            # Юзер удалён из БД — сбрасываем залипшие флаги из persistence-кеша
            context.user_data["phone_confirmed"] = False
            context.user_data["_consent_ok"] = False
            context.user_data["_verified"] = False
            context.user_data.pop("phone", None)
            context.user_data.pop("inn", None)
            # fallback: если отдельный метод по ИНН всё ещё нужен
            inn_db = await get_user_inn(uid)
            if inn_db:
                context.user_data["inn"] = inn_db

        # сбрасываем "одноразовые" режимы, которые не должны переживать рестарты
        for k in ("finish_verify_mode", "extra_verify_offer", "extra_verify_done"):
            # не трогаем, если их выставили в этом апдейте явно
            if k in context.user_data and context.user_data.get(k) in (True, False, None):
                # оставим, но если пользователь уже верифицирован — точно не нужно
                if context.user_data.get("_verified"):
                    context.user_data.pop(k, None)

    except Exception as e:
        logger.warning("hydrate_user_cache failed for %s: %s", uid, e)



def is_registered(context: ContextTypes.DEFAULT_TYPE) -> bool:
    name_ok = bool((context.user_data.get("first_name") or "").strip())
    # телефон должен содержать >=10 цифр
    phone_raw = context.user_data.get("phone") or ""
    phone_ok = sum(ch.isdigit() for ch in phone_raw) >= 10
    # ИНН = 10 или 12 цифр
    inn_len = len("".join(ch for ch in (context.user_data.get("inn") or "") if ch.isdigit()))
    inn_ok = inn_len in (10, 12)
    return name_ok and phone_ok and inn_ok


def kb_consent():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Принять условия", callback_data="accept_consent")],
                                 [InlineKeyboardButton("❌ Продолжить без регистрации", callback_data="go_guest")]])

def _get_reg_lock(context: ContextTypes.DEFAULT_TYPE, uid: str) -> asyncio.Lock:
    locks = context.application.bot_data.setdefault("reg_locks", {})
    if uid not in locks:
        locks[uid] = asyncio.Lock()
    return locks[uid]


async def show_consent_once(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать экран согласия через новый ui_flow слой."""
    # антидубликат: если прямо сейчас уже рисуем согласие — выходим
    if context.user_data.get("_consent_inflight"):
        return
    context.user_data["_consent_inflight"] = True

    try:
        text = (
            "Продолжая работу с ботом @yurtaCRM_bot, вы подтверждаете согласие "
            "на обработку персональных данных."
        )
        kb = kb_consent()

        await _ui.render_screen(update, context, text, reply_markup=kb)
        context.user_data["_consent_shown"] = True

    finally:
        context.user_data["_consent_inflight"] = False


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await show_main_menu(update, context, "Действие отменено.")


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]])


def kb_ident_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📲 Поделиться номером (рекомендуется)", callback_data="ident_share_contact")],
        [InlineKeyboardButton("🧾 Ввести ИНН", callback_data="ident_enter_inn")],
        [InlineKeyboardButton("❌ Позже", callback_data="ident_cancel")],
    ])



async def push_extra_verification_to_crm_async(context, update: Update):
    """
    Отправляем запрос «ДОП ВЕРИФИКАЦИЯ» в Django CRM.
    """
    ud = context.user_data or {}

    first = (ud.get("first_name") or "").strip()
    last = (ud.get("last_name") or "").strip()
    phone = (ud.get("phone") or "").strip()
    email = (ud.get("email") or "").strip()
    inn = (ud.get("inn") or "").strip()
    city = (ud.get("city") or "").strip()
    uname = update.effective_user.username or ""
    uid = update.effective_user.id

    user_data = {
        "first_name": first,
        "last_name": last,
        "phone": phone,
        "email": email,
        "inn": inn,
        "city": city,
        "tg_username": uname,
        "tg_user_id": uid,
    }

    await push_verification_to_crm(user_data, source="telegram")




# ===== Старт / регистрация / согласие / идентификация =====
# Перенести сюда реализацию:

async def send_welcome_hero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (
        "Давайте знакомиться, меня зовут Юрта_бот!\n\n"
    )
    try:
        if WELCOME_IMAGE_PATH.exists():
            # показываем “отправляет фото”
            try:
                await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
            except Exception as e:
                print(e)
                pass

            with open(WELCOME_IMAGE_PATH, "rb") as f:
                m = await update.effective_chat.send_photo(
                    photo=f, caption=caption, parse_mode=ParseMode.HTML
                )
                reg_track_mid(context, int(m.chat_id), int(m.message_id))

        else:
            # показываем “печатает…”
            try:
                await update.effective_chat.send_action(ChatAction.TYPING)
            except Exception as e:
                print(e)
                pass

            m = await update.effective_chat.send_message(
                caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            reg_track_mid(context, int(m.chat_id), int(m.message_id))

    except Exception as e:
        print(e)
        # ловим и таймауты/сеть тоже, чтобы не ломало регистрацию
        import re as _re
        plain = _re.sub(r"<[^>]+>", "", caption)
        await update.effective_chat.send_message(plain, disable_web_page_preview=True)


async def _show_subscription_required_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает экран «подпишитесь на канал» и возвращает S1_IDENT."""
    channel = TG_REQUIRED_CHANNEL
    channel_link = f"https://t.me/{channel.lstrip('@')}" if channel.startswith("@") else channel
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=channel_link)],
        [InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription")],
    ])
    await send_or_edit(
        update,
        "📢 Для использования бота необходимо подписаться на наш канал.\n\n"
        "1️⃣ Нажмите «Подписаться на канал»\n"
        "2️⃣ Нажмите «Я подписался» для продолжения",
        kb,
        parse_mode=None,
        context=context,
    )
    return S1_IDENT


async def cb_check_subscription_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback «Я подписался» — проверяет подписку повторно."""
    q = update.callback_query
    if q:
        await q.answer()
    uid = str(update.effective_user.id)
    subscribed = await is_subscribed_tg(context.application.bot, int(uid))
    if not subscribed:
        await send_or_edit(
            update,
            "❌ Подписка не обнаружена. Пожалуйста, подпишитесь на канал и нажмите «Я подписался» снова.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{TG_REQUIRED_CHANNEL.lstrip('@')}"
                                     if TG_REQUIRED_CHANNEL.startswith("@") else TG_REQUIRED_CHANNEL)],
                [InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription")],
            ]),
            parse_mode=None,
            context=context,
        )
        return S1_IDENT
    return await start_registration(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Входная точка (/start):
    - показываем приветствие (с дебаунсом)
    - дальше ВСЮ логику шага регистрации отдаём start_registration()
    """
    # чистим только то, что должно быть "сессионным"
    for k in (
        "cart", "form", "co_mode",
        "finish_verify_mode",          # режим "дозавершить"
        "extra_verify_offer", "extra_verify_done",
        # consent/UI флаги могли сохраниться в persistence и "залипнуть"
        "_consent_shown", "_consent_inflight", "_consent_msg_id",
        # НЕ трогаем skipped_inn — это осознанный выбор пользователя
    ):
        context.user_data.pop(k, None)
    try:
        await clear_user_notices_tg(
            tg_bot=context.application.bot,
            tg_chat_id=int(update.effective_user.id),
            user_id=int(update.effective_user.id),
        )
    except Exception:
        logger.debug("best-effort /start notice cleanup failed for uid=%s", update.effective_user.id, exc_info=True)

    uid = str(update.effective_user.id)
    await hydrate_user_cache(context, uid)

    # TG deep-link payload: /start ref_XXXX
    payload = ""
    start_text = ""
    try:
        start_text = (update.effective_message.text or "").strip() if update.effective_message else ""
        if getattr(context, "args", None):
            payload = (context.args[0] or "").strip()
        elif update.effective_message and update.effective_message.text:
            parts = update.effective_message.text.strip().split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ""
    except Exception:
        payload = ""

    logger.info(
        "[REF-TG] /start received: tg_user_id=%s text=%r payload=%r args=%r",
        uid,
        start_text,
        payload,
        getattr(context, "args", None),
    )

    if payload.startswith("ref_"):
        ref_code = payload[4:].strip()
        if ref_code:
            try:
                logger.info(
                    "[REF-TG] /start referral candidate: tg_user_id=%s payload=%r ref_code=%r link_hint=%r",
                    uid,
                    payload,
                    ref_code,
                    f"https://t.me/{TG_REF_BOT_USERNAME}?start={payload}" if TG_REF_BOT_USERNAME else "",
                )
                res = await apply_referral_code(int(uid), raw_code=ref_code, raw_text=payload, source_platform="telegram")
                logger.info(
                    "[REF-TG] /start referral apply result: tg_user_id=%s payload=%r result=%s",
                    uid,
                    payload,
                    res,
                )
            except Exception:
                logger.exception("TG start referral apply failed: uid=%s payload=%s", uid, payload)
        else:
            logger.info("[REF-TG] /start referral ignored: empty ref_code after payload parse, uid=%s payload=%r", uid, payload)
    elif payload:
        logger.info("[REF-TG] /start payload present but not referral: uid=%s payload=%r", uid, payload)

    # welcome (не чаще раза в минуту)
    now = time.time()
    last_welcome = context.user_data.get("_welcome_sent_ts", 0)
    sent_welcome = False
    if now - last_welcome > 60:
        context.user_data["_welcome_sent_ts"] = now
        await send_welcome_hero(update, context)
        sent_welcome = True

    # ⏸ пауза только если реально показали приветствие
    if sent_welcome:
        await asyncio.sleep(2)

    # Проверка подписки на канал (если TG_REQUIRED_CHANNEL задан)
    if TG_REQUIRED_CHANNEL:
        subscribed = await is_subscribed_tg(context.application.bot, int(uid))
        if not subscribed:
            return await _show_subscription_required_tg(update, context)

    return await start_registration(update, context)


async def on_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Пользователь ввёл имя.
    - track_user_input ставит сообщение на удаление через TTL
    - сохраняем имя в БД и user_data
    - передаём управление в start_registration
    """
    msg_in = update.effective_message
    if msg_in:
        await _ui.track_user_input(update, context)

    name = (msg_in.text or "").strip() if msg_in else ""
    uid = str(update.effective_user.id)

    if name:
        context.user_data["first_name"] = name
        await upsert_user(uid, first_name=name)

    return await start_registration(update, context)


async def ident_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    uid = str(update.effective_user.id)

    # если ещё нет согласия — сначала текст согласия
    if not (await _has_consent(uid)):
        return await start_registration(update, context)

    # согласие уже есть — сразу просим подтвердить телефон
    await send_or_edit(
        update,
        "Для регистрации нужно подтвердить номер телефона через Telegram:",
        kb_request_contact()
    )
    return S2_WAIT_CONTACT


async def show_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    text = (
        "Регистрируясь в сервисе, вы подтверждаете согласие на обработку, "
        "передачу и хранение ваших персональных данных, а также на получение "
        "сервисных уведомлений."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтверждаю", callback_data="accept_consent")],
        [InlineKeyboardButton("❌ Продолжить без регистрации", callback_data="go_guest")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="back_to_main")],
    ])
    if q:
        await q.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_chat.send_message(text, reply_markup=kb)
    # ВАЖНО: дальше ловим accept_consent/go_guest в cb_consent (S1_IDENT)
    return S1_IDENT


async def show_home_limited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Гостевой экран оставляем для случаев явного входа «как гость» (кнопка go_guest)
    или когда нет согласия. Используем send_or_edit, чтобы не дублировать сообщения.
    """
    rows = [
        [
            InlineKeyboardButton("💬 Мои обращения", callback_data="cc_my_chats"),
            InlineKeyboardButton(f"🛒 Корзина ({len(context.user_data.get('cart', [])) or 0})", callback_data="cart"),
        ],
        [InlineKeyboardButton("🧰 Услуги", callback_data="services")],
        [InlineKeyboardButton("🧾 Бухгалтерия / Отчётность", callback_data="client_chat_start:0")],
        [InlineKeyboardButton(f"{RU_FLAG} Магазин уЮрты", callback_data="buy")],
        [InlineKeyboardButton("🛠 Техническая поддержка", callback_data="client_chat_start:1")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
    ]

    if is_admin(update.effective_user.id):
        rows.append([InlineKeyboardButton("🛠 Админка", callback_data="admin_menu")])

    await send_or_edit(
        update,
        # текст можно оставить прежним — он больше не появляется «после регистрации»,
        # а только при реальном гостевом входе
        "Вы в гостевом режиме. Каталог и заявка доступны.\n"
        "Когда решите оформить покупку — подскажем, какие данные нужно подтвердить.",
        InlineKeyboardMarkup(rows)
    )


async def cb_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обработка согласия / гостевого режима:
    - accept_consent: сохраняем согласие, логируем, продолжаем регистрацию
    - go_guest: уходим в гостевой режим без лишней возни
    """
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "accept_consent":
        uid = str(update.effective_user.id)
        await upsert_user(uid, tg_username=update.effective_user.username or "")
        try:
            await set_user_consent(uid, CONSENT_VERSION)
            import datetime as _dt
            logger.info(
                "consent_accept: uid=%s, version=%s, ts=%s, username=%s",
                uid,
                CONSENT_VERSION,
                _dt.datetime.now().isoformat(),
                update.effective_user.username or "",
            )
            if context.user_data is not None:
                context.user_data["_consent_ok"] = True
        except Exception as e:
            logger.warning("set_user_consent failed: %s", e)

        # аккуратно закрываем текст согласия
        if q and q.message:
            reg_track_mid(context, int(q.message.chat_id), int(q.message.message_id))

        await q.edit_message_text("Спасибо за доверие, продолжаем.")
        context.user_data["_consent_shown"] = False

        context.user_data.pop("_consent_msg_id", None)

        # 👉 дальше ВСЮ логику (имя / телефон / ИНН) отдаём start_registration
        return await start_registration(update, context)

    if data == "go_guest":
        # Явно помечаем, что согласия нет
        if context.user_data is not None:
            context.user_data["_consent_ok"] = False

        # Сразу показываем гостевое меню, без промежуточного текста
        await show_home_limited(update, context)
        context.user_data.pop("_consent_msg_id", None)
        context.user_data["_consent_shown"] = False
        return S0_GUEST


async def cb_ident(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Оставляем кнопки, но поведение – «собрать оба поля обязательно».
    """
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "ident_share_contact":
        await q.edit_message_text("Поделитесь номером телефона:")
        await update.effective_chat.send_message("Нажмите кнопку ниже:", reply_markup=kb_request_contact())
        return S2_WAIT_CONTACT

    if data == "ident_enter_inn":
        await q.edit_message_text("Укажите ИНН (10 или 12 цифр):")
        return S4_WAIT_INN

    if data == "ident_cancel":
        await q.edit_message_text("Ок, вернёмся в гостевой режим.")
        await show_home_limited(update, context)
        return S0_GUEST


async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Прилетел контакт — сохраняем телефон.
    - track_user_input ставит сообщение на удаление через TTL
    """
    m_in = update.effective_message
    if m_in:
        await _ui.track_user_input(update, context)

    if not update.effective_message.contact:
        return S1_IDENT

    phone = _normalize_phone(update.effective_message.contact.phone_number)
    if not phone:
        await _ui.render_notice(update, context, "Не удалось распознать номер. Попробуйте ещё раз кнопкой ниже.", reply_markup=kb_request_contact())
        return S2_WAIT_CONTACT

    uid = str(update.effective_user.id)

    # ── Распознавание специалиста по номеру телефона ─────────────────────────
    # Выполняется ДО проверки consent: после /delete_me юзер отсутствует в
    # users, _has_consent вернёт False и уйдёт в start_registration раньше,
    # чем мы успеем найти специалиста по телефону.
    specialist = None
    try:
        specialist = await get_specialist_by_phone(phone)
        logger.info(
            "[SPEC-TG] on_contact: uid=%s phone=%r → spec_id=%s profession=%s role=%s is_active=%s is_partner=%s",
            uid, phone,
            (specialist or {}).get("id"), (specialist or {}).get("profession"),
            (specialist or {}).get("role"), (specialist or {}).get("is_active"),
            is_partner_specialist(specialist) if specialist else None,
        )
    except Exception as _spe:
        logger.warning("[SPEC-TG] on_contact: specialist by phone lookup failed: uid=%s phone=%r error=%s", uid, phone, _spe)

    if specialist and not is_partner_specialist(specialist):
        # Наш внутренний специалист (profession 0-4, не партнёр)
        await upsert_user(uid, phone=phone, tg_username=update.effective_user.username or "")
        context.user_data["phone"] = phone
        context.user_data["phone_confirmed"] = True
        try:
            await set_specialist_phone_verified(int(specialist["id"]), True)
            await link_specialist_tg_user(
                int(specialist["id"]),
                int(uid),
                update.effective_user.username or None,
            )
        except Exception as _sle:
            logger.warning("TG on_contact: specialist tg link/verify failed: %s", _sle)

        first_name = (context.user_data.get("first_name") or "").strip() or "Коллега"
        await _ui.render_notice(
            update, context,
            f"Номер подтверждён ✅ <code>{phone}</code>\n\n"
            f"{first_name}, вы вошли как специалист. Открываю список обращений…",
            parse_mode=ParseMode.HTML,
        )
        from bot_agent_chat import _set_agent_ctx, _render_chat_list
        _set_agent_ctx(context, specialist, int(uid))
        return await _render_chat_list(update, context)

    # ─────────────────────────────────────────────────────────────────────────
    # Не специалист (или партнёр) — продолжаем клиентский flow
    if not (await _has_consent(uid)):
        return await start_registration(update, context)

    await upsert_user(uid, phone=phone, tg_username=update.effective_user.username or "")
    context.user_data["phone"] = phone
    context.user_data["phone_confirmed"] = True

    # Если это партнёр — сразу гарантируем постоянный чат с директором
    try:
        from db.users import resolve_internal_user_id as _resolve_uid
        _internal_uid = await _resolve_uid(tg_user_id=int(uid))
        _identity = await resolve_partner_identity_by_phone(
            internal_user_id=_internal_uid,
            tg_user_id=int(uid),
            phone=phone,
        )
        if _identity.get("partner_detected"):
            _canonical_uid = int(_identity.get("canonical_user_id") or 0)
            if _canonical_uid > 0:
                await ensure_partner_entrypoint_chat(
                    partner_user_id=_canonical_uid,
                    tg_user_id=int(uid),
                    phone=phone,
                    source="telegram_registration",
                )
    except Exception as _pce:
        logger.warning("TG on_contact: partner entrypoint chat setup failed: %s", _pce)

    await _ui.render_notice(update, context, f"Номер подтверждён ✅ <code>{phone}</code>", parse_mode=ParseMode.HTML)
    await asyncio.sleep(1)

    inn = (context.user_data.get("inn") or "").strip()
    if not inn:
        first_name = (context.user_data.get("first_name") or "").strip()
        if not first_name:
            first_name = (update.effective_user.first_name or "").strip() or "Коллега"

        await _ui.render_notice(update, context, f"{first_name}, спасибо за доверие, продолжим.\n\nПолный функционал реализован для наших действующих клиентов.")
        await asyncio.sleep(2)

    return await start_registration(update, context)


async def on_text_inn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()

    # сообщение с ИНН — track_user_input
    m_in = update.effective_message
    if m_in:
        await _ui.track_user_input(update, context)

    if not _looks_like_inn(text):
        await _ui.render_notice(update, context, "ИНН должен содержать 10 или 12 цифр. Попробуйте снова.")
        return S4_WAIT_INN

    inn = _digits(text)
    context.user_data["inn"] = inn
    context.user_data.pop("skipped_inn", None)
    await set_user_inn(str(update.effective_user.id), inn)

    await _ui.render_notice(update, context, "✅ ИНН принят!")
    await asyncio.sleep(2)

    return await start_registration(update, context)


async def skip_inn_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Пользователь нажал «Ввести позже» — ИНН не заполняем,
    просто выводим финальное приветствие и главное меню,
    без повторной «регистрации».
    """
    q = update.callback_query
    if q:
        await q.answer()

    uid = str(update.effective_user.id)
    context.user_data.setdefault("extra_verify_offer", False)
    # ⚠️ помечаем, что пользователь осознанно пропустил ИНН
    context.user_data["skipped_inn"] = True

    # имя для обращения
    first_name = (context.user_data.get("first_name") or "").strip()
    if not first_name:
        try:
            u = await get_user(uid) or {}
            first_name = (u.get("first_name") or "").strip()
        except Exception as e:
            print(e)
            first_name = ""
    if not first_name:
        first_name = (update.effective_user.first_name or "").strip() or "Коллега"

    text = (
        "Благодарим за выбор нашей компании.\n\n"
        f"{first_name}, далее выбирайте интересующий вас пункт меню, расположенного ниже.\n\n"
        "Чем ещё могу вам помочь?"
    )

    res = await show_main_menu(update, context, text)

    keep_mid = int(context.user_data.get("_menu_msg_id") or 0)
    await reg_cleanup_messages(
        context,
        int(update.effective_chat.id),
        keep_ids={keep_mid} if keep_mid else set()
    )
    return res


async def show_home_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полноценное меню без создания нового сообщения — редактируем текущий экран."""
    await send_or_edit(
        update,
        "Готово. Полный доступ открыт.",
        await nav_kb(context, update.effective_user.id)
    )


async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = str(update.effective_user.id)
    lock = _get_reg_lock(context, uid)

    async with lock:
        await hydrate_user_cache(context, uid)

        if update.callback_query and (update.callback_query.data == "register"):
            context.user_data["finish_verify_mode"] = True

        phone_conf = bool(context.user_data.get("phone_confirmed"))
        inn = (context.user_data.get("inn") or "").strip()

        if phone_conf and context.user_data.get("skipped_inn") and not context.user_data.get("finish_verify_mode"):
            return await show_main_menu(
                update,
                context,
                "Вы уже можете пользоваться меню. ИНН можно указать позже через «Мой профиль»."
            )

        # 1) Согласие
        if not (await _has_consent(uid)):
            await show_consent_once(update, context)
            return S1_IDENT

        # 2) Специалист (не директор и не партнёр) — перенаправить в режим специалиста.
        #    Срабатывает даже если специалист уже был зарегистрирован как клиент
        #    (в этом случае on_contact не вызывается и без этой проверки они падают в клиентское меню).
        try:
            spec_check = await get_specialist_by_tg(
                tg_user_id=int(uid),
                tg_username=(update.effective_user.username or "").strip() or None,
            )
            if spec_check and not is_partner_specialist(spec_check):
                _role = str((spec_check or {}).get("role") or "specialist")
                _profession = int((spec_check or {}).get("profession") or 0)
                # profession=2 / is_director → директор, пропускаем (имеет доступ к клиентскому меню)
                if _role != "director" and not (int((spec_check or {}).get("is_director") or 0) == 1 or _profession == 2):
                    from bot_agent_chat import agent_entry
                    return await agent_entry(update, context)
        except Exception as _se:
            logger.warning("start_registration: specialist check failed: %s", _se)

        # 3) Имя
        if not (context.user_data.get("first_name") or "").strip():
            await _ui.render_screen(update, context, "Как вас зовут?")
            return S0_START

        # 3) Телефон
        if not phone_conf:
            await _ui.render_notice(update, context, "Для регистрации нужно подтвердить номер телефона и указать ИНН.")
            await asyncio.sleep(2)
            await _ui.render_screen(update, context, "Сначала подтвердите номер телефона через Telegram:", reply_markup=kb_request_contact())
            return S2_WAIT_CONTACT

        # 4) ИНН
        if not inn:
            await _ui.render_notice(update, context, "Давайте проверим, работали мы с вами ранее или нет.")
            await asyncio.sleep(2)
            await _ui.render_screen(
                update, context,
                "Для этого нам понадобится ваш ИНН (10 или 12 цифр).\n\n"
                "Если ИНН сейчас под рукой нет, нажмите «Ввести позже».",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Ввести позже", callback_data="skip_inn")],
                ])
            )
            return S4_WAIT_INN

        # 5) Финал
        context.user_data.pop("finish_verify_mode", None)
        return await verify_and_finish(update, context)


async def verify_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Финальный шаг регистрации/верификации.

    Правила (после миграции):
    - Если у пользователя есть подтверждённый телефон + ИНН -> считаем верификацию завершённой ВСЕГДА.
    - Если в базе есть заранее заведённая карточка клиента и данные совпали -> выполняем сведение (merge).
    - Если совпало только одно поле (телефон или ИНН) -> показываем предупреждение "проверьте введённые данные".
    Проверка на сведение выполняется при каждом обновлении телефона/ИНН (через вызовы verify_and_finish).
    """
    uid = str(update.effective_user.id)
    phone = (context.user_data.get("phone") or "").strip()
    inn = (context.user_data.get("inn") or "").strip()
    confirmed = bool(context.user_data.get("phone_confirmed"))

    # Имя для обращений
    first_name = (context.user_data.get("first_name") or "").strip()
    if not first_name:
        first_name = (update.effective_user.first_name or "").strip() or "Коллега"

    text_verified = (
        "✅ Верификация завершена.\n\n"
        f"{first_name}, далее выбирайте интересующий вас пункт меню, расположенного ниже.\n\n"
        "Чем ещё могу вам помочь?"
    )

    # если нет подтверждённого телефона или ИНН — просто выводим меню (дальше допросят)
    if not confirmed or not inn:
        context.user_data["extra_verify_offer"] = False
        res = await show_main_menu(update, context, text_verified)
        keep_mid = int(context.user_data.get("_menu_msg_id") or 0)
        await reg_cleanup_messages(
            context,
            int(update.effective_chat.id),
            keep_ids={keep_mid} if keep_mid else set()
        )
        return res

    # 1) Пытаемся свести с предзагруженной карточкой клиента (если есть)
    merge_note = None
    try:
        merge_res = await try_merge_user_with_preloaded(int(uid), inn, phone)
        st = (merge_res or {}).get("status")
        note = (merge_res or {}).get("note")
        if st == "merged":
            merge_note = "🔗 Профиль синхронизирован с вашей карточкой клиента."
        elif st == "mismatch_inn":
            merge_note = "⚠️ В базе найден клиент с таким ИНН, но другим телефоном. Проверьте телефон в профиле."
        elif st == "mismatch_phone":
            merge_note = "⚠️ В базе найден клиент с таким телефоном, но другим ИНН. Проверьте ИНН в профиле."
        elif st == "ambiguous":
            merge_note = "⚠️ Найдено несколько совпадений в базе. Напишите в поддержку для ручной проверки."
        elif st == "conflict":
            merge_note = "⚠️ Эти данные уже привязаны к другому Telegram. Напишите в поддержку."
        elif note:
            merge_note = str(note)
    except Exception as e:
        logger.exception("try_merge_user_with_preloaded error: %s", e)

    # 2) В любом случае ставим verified + consent
    try:
        await mark_user_verified(uid)
    except Exception as e:
        logger.exception("mark_user_verified failed for %s: %s", uid, e)

    try:
        await set_user_consent(uid, CONSENT_VERSION)
    except Exception as e:
        logger.exception("set_user_consent failed for %s: %s", uid, e)

    context.user_data["extra_verify_offer"] = False
    context.user_data["_verified"] = True
    context.user_data["_consent_ok"] = True

    # После сведения могли измениться поля в users -> перезагрузим кэш
    try:
        await hydrate_user_cache(context, uid)
    except Exception:
        pass

    # сообщение + меню
    final_text = text_verified
    if merge_note:
        final_text = f"{merge_note}\n\n{final_text}"

    if context.user_data.pop("co_mode", None) == "checkout":
        return await checkout(update, context)

    res = await show_main_menu(update, context, final_text)

    keep_mid = int(context.user_data.get("_menu_msg_id") or 0)
    await reg_cleanup_messages(
        context,
        int(update.effective_chat.id),
        keep_ids={keep_mid} if keep_mid else set()
    )
    return res



async def extra_verify_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()

    uid = str(update.effective_user.id)
    await hydrate_user_cache(context, uid)

    # если уже отправляли — вторую заявку не создаём
    if context.user_data.get("extra_verify_done"):
        await send_or_edit(
            update,
            "Вы уже отправили заявку на дополнительную верификацию. Мы всё проверим и свяжемся с вами.",
            await nav_kb(context, update.effective_user.id)
        )
        return STATE_IDLE

    ud = context.user_data

    missing = []
    if not (ud.get("first_name") or "").strip():
        missing.append("имя")
    if not (ud.get("last_name") or "").strip():
        missing.append("фамилия")
    if not (ud.get("phone") or "").strip():
        missing.append("телефон")
    if not (ud.get("inn") or "").strip():
        missing.append("ИНН")
    if not (ud.get("email") or "").strip():
        missing.append("e-mail")
    if not (ud.get("city") or "").strip():
        missing.append("город")

    if missing:
        text = (
                "Чтобы отправить данные на дополнительную верификацию, нужно заполнить профиль:\n\n"
                + "\n".join(f"• {m}" for m in missing) +
                "\n\nОткройте «👤 Мой профиль» → «✏️ Редактировать профиль» и заполните недостающие поля.\n"
                "После этого снова нажмите «Дополнительная верификация»."
        )
        await send_or_edit(update, text, nav_kb(context, update.effective_user.id))
        return STATE_IDLE

    if (await is_specialist_user(update, context)):
        await update.effective_chat.send_message(
            "🧑‍🔧 Режим специалиста: не связываемся с CRM."
        )
        from bot_agent_chat import agent_entry
        return await agent_entry(update, context)
    # все данные есть — шлём в Django CRM
    try:
        await push_extra_verification_to_crm_async(context, update)
    except Exception as e:
        await send_or_edit(
            update,
            f"Не удалось отправить данные в CRM: {e}",
            await nav_kb(context, update.effective_user.id)
        )
        return STATE_IDLE

    context.user_data["extra_verify_done"] = True
    context.user_data["extra_verify_offer"] = False

    await send_or_edit(
        update,
        "✅ Заявка на дополнительную верификацию отправлена.\n"
        "Мы проверим данные и при необходимости свяжемся с вами.",
        await nav_kb(context, update.effective_user.id)
    )
    return STATE_IDLE


async def profile_finish_verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()

    uid = str(update.effective_user.id)
    await hydrate_user_cache(context, uid)
    ud = context.user_data or {}

    # 0) Проверяем, что профиль заполнен "по-взрослому"
    missing: list[str] = []

    if not (ud.get("first_name") or "").strip():
        missing.append("имя")
    if not (ud.get("last_name") or "").strip():
        missing.append("фамилия")
    if not (ud.get("phone") or "").strip():
        missing.append("телефон")
    if not ud.get("phone_confirmed"):
        missing.append("подтверждение телефона через Telegram")
    if not (ud.get("inn") or "").strip():
        missing.append("ИНН")
    if not (ud.get("email") or "").strip():
        missing.append("e-mail")
    if not (ud.get("city") or "").strip():
        missing.append("город")

    if missing:
        text = (
                "Чтобы завершить верификацию, нужно полностью заполнить профиль:\n\n"
                + "\n".join(f"• {m}" for m in missing) +
                "\n\nОткройте «👤 Мой профиль» → «✏️ Редактировать профиль» и заполните недостающие поля.\n"
                "После этого снова нажмите «🔐 Завершить верификацию»."
        )
        await send_or_edit(update, text, await nav_kb(context, update.effective_user.id))
        return STATE_IDLE

    # 1) Дальше логика как раньше: сначала пытаемся обычную верификацию по ИНН+телефон
    inn = (ud.get("inn") or "").strip()
    phone_conf = bool(ud.get("phone_confirmed"))

    if phone_conf and inn:
        await verify_and_finish(update, context)
        # проверяем флаг верификации по факту
        if not await is_user_verified(uid):
            try:
                await push_extra_verification_to_crm_async(context, update)
                context.user_data["extra_verify_done"] = True
                context.user_data["extra_verify_offer"] = False
                await update.effective_chat.send_message(
                    "✅ Заявка на дополнительную верификацию отправлена.\n"
                    "Мы проверим данные и при необходимости свяжемся с вами."
                )
            except Exception as e:
                logger.error(f"Ошибка отправки в Django CRM (доп. верификация): {e}")
                await update.effective_chat.send_message(
                    "⚠️ Произошла ошибка при отправке данных. Пожалуйста, попробуйте позже."
                )
        return STATE_IDLE

    # 2) Телефон не подтверждён — просим контакт через Telegram
    if not phone_conf:
        await update.effective_chat.send_message(
            "Чтобы завершить верификацию, подтвердите номер телефона через Telegram:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📲 Поделиться номером", request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
                input_field_placeholder="Нажмите кнопку ниже"
            )
        )
        return S2_WAIT_CONTACT

    # 3) Телефон подтверждён, но нет ИНН — просим ИНН
    if not inn:
        await send_or_edit(
            update,
            "Укажите ИНН (10 или 12 цифр):",
            InlineKeyboardMarkup([])
        )
        return S4_WAIT_INN

    # 4) fallback (на всякий случай)
    await update.effective_chat.send_message(
        "Чтобы показать персональные данные, подтвердите личность:",
        reply_markup=kb_ident_menu()
    )
    return STATE_IDLE



def _parse_date_only(raw):
    """Parse date from 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS' or datetime objects."""
    import datetime
    if not raw:
        return None
    if isinstance(raw, datetime.datetime):
        return raw.date()
    if isinstance(raw, datetime.date):
        return raw
    s = str(raw).strip()
    if len(s) >= 10:
        s10 = s[:10]
        try:
            return datetime.date.fromisoformat(s10)
        except Exception:
            pass
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


# ===== Профиль / "Мои покупки" / ОФД =====
import hashlib

async def show_referral_qr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Показ реферального QR-кода.

    Важно:
    - QR — это фото (media), поэтому рисуем НОВЫМ сообщением.
    - Старый экран (профиль) удаляем ПОСЛЕ успешной отправки QR, чтобы не оставлять чат пустым.
    - Файл генерируем один раз и дальше переиспользуем.
    """
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    def _build_referral_url(platform: str, code: str) -> str:
        if platform == "max":
            bot_name = (MAX_REF_BOT_NAME or MAX_BOT_NAME or MAX_REF_FALLBACK_BOT_NAME or "").strip().lstrip("@")
            return f"https://max.ru/{bot_name}?start=ref_{code}" if bot_name else ""
        return f"https://t.me/{TG_REF_BOT_USERNAME}?start=ref_{code}" if TG_REF_BOT_USERNAME else ""

    # Реферальный QR доступен только верифицированным пользователям
    if not await is_user_verified(str(update.effective_user.id)):
        await send_or_edit(
            update,
            "⛔ Реферальный QR доступен только после верификации профиля.\n\n"
            "Подтвердите телефон и укажите ИНН.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
                [InlineKeyboardButton("👤 В профиль", callback_data="profile")],
            ]),
            context=context,
        )
        return STATE_IDLE

    data = ((q.data if q else "") or "").strip()
    selected_platform = ""
    if data.startswith("my_referral_qr:"):
        selected_platform = data.split(":", 1)[1].strip().lower()

    if selected_platform not in ("telegram", "max"):
        await send_or_edit(
            update,
            "Выберите платформу для QR-кода:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Telegram", callback_data="my_referral_qr:telegram")],
                [InlineKeyboardButton("MAX", callback_data="my_referral_qr:max")],
                [InlineKeyboardButton("⬅ В профиль", callback_data="profile")],
            ]),
            context=context,
        )
        return STATE_IDLE

    # старое сообщение (обычно профиль/выбор платформы), которое можно удалить после отправки QR
    old_msg = q.message if (q and q.message) else None

    # 1) реф-код (для партнёра profession=5 — единый specialist ref_code)
    tg_uid = int(update.effective_user.id)
    username = (update.effective_user.username or "").strip()
    specialist = await get_specialist_by_tg(tg_user_id=tg_uid, tg_username=username or None)
    if specialist and int(specialist.get("profession") or 0) == 5:
        specialist_id = int(specialist.get("id") or 0)
        owner_name = (specialist.get("name") or "").strip() or f"Специалист #{specialist_id}"
        ref_code = await get_or_create_specialist_ref_code(specialist_id, owner_name=owner_name)
    else:
        ref_code = await get_user_ref_code(tg_uid)
        if not ref_code:
            ref_code = await create_user_ref_code(tg_uid)

    referral_url = _build_referral_url(selected_platform, ref_code)
    if not referral_url:
        await send_or_edit(
            update,
            "⚠️ Не настроена ссылка для выбранной платформы.",
            context=context,
        )
        return STATE_IDLE

    qr_namespace = TG_QR_NAMESPACE if selected_platform == "telegram" else MAX_QR_NAMESPACE

    # 2) файл QR (хэш-нэйминг, единый источник — generate_qr_file_hashed)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    need_dir = os.path.join(base_dir, "images", "qrs")
    os.makedirs(need_dir, exist_ok=True)

    qr_path = None
    try:
        # ожидаем, что функция вернёт путь; если нет — сделаем fallback ниже
        qr_path = generate_qr_file_hashed(
            referral_url,
            out_dir=need_dir,
            namespace=qr_namespace,
        )
    except Exception:
        qr_path = None

    # fallback: md5 от url (совместимость со старой логикой)
    if not qr_path:
        qr_hash_base = f"{qr_namespace}|{referral_url}"
        qr_path = os.path.join(need_dir, f"{hashlib.md5(qr_hash_base.encode('utf-8')).hexdigest()}.png")
        if not os.path.exists(qr_path):
            try:
                generate_qr_file_hashed(
                    referral_url,
                    out_dir=need_dir,
                    namespace=qr_namespace,
                )
            except Exception:
                pass

    # 3) отправка
    try:
        with open(qr_path, "rb") as f:
            sent = await update.effective_chat.send_photo(
                photo=f,
                caption=(
                    "Вы можете поделиться этим QR-кодом, позвав к нам новых клиентов "
                    "и получив дополнительные бонусы!\n\n"
                    f"Ссылка для приглашения:\n{referral_url}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ В профиль", callback_data="profile")],
                ]),
            )
    except Exception as e:
        # если не получилось — хотя бы вернём пользователя в профиль/меню
        try:
            await show_main_menu(update, context, "Не удалось показать QR-код. Меню обновлено.")
        except Exception:
            pass
        return STATE_IDLE

    # 4) теперь безопасно чистим старый экран
    if old_msg:
        try:
            await old_msg.delete()
        except Exception:
            pass

    return STATE_IDLE

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    import datetime

    user = update.effective_user
    uid = str(user.id)

    try:
        profile = await get_user(uid) or {}
    except Exception as e:
        print(e)
        profile = {}

    first_name = (profile.get("first_name") or user.first_name or "").strip()
    last_name = (profile.get("last_name") or user.last_name or "").strip()
    email = (profile.get("email") or context.user_data.get("email") or "").strip()
    city = (profile.get("city") or context.user_data.get("city") or "").strip()
    inn = (profile.get("inn") or context.user_data.get("inn") or "").strip()
    phone = (profile.get("phone") or context.user_data.get("phone") or "").strip()

    # флаг доп. верификации по умолчанию
    context.user_data.setdefault("extra_verify_done", False)

    verified = await is_user_verified(uid)

    def _fmt_date(raw_date: str, today: datetime.date) -> str:
        """Форматирует дату: 01.01.2026 (🟩 120 дн.)"""
        if not raw_date:
            return "нет данных"
        try:
            d = _parse_date_only(raw_date)
            if not d:
                raise ValueError('bad date')
            left = (d - today).days
            badge = _days_badge(left)
            return f"{d:%d.%m.%Y} ({badge})"
        except Exception:
            return str(raw_date)

    # блок сроков по ККТ (ОФД + ФН + Маркировка + адреса)
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
                "profile_kkt_access platform=tg tg_user_id=%s internal_id=%s kkt_count=%s",
                uid, internal_id, len(kkt_rows),
            )
            if kkt_rows:
                today = datetime.date.today()
                slot_blocks: list[str] = []

                for kkt in kkt_rows:
                    slot = kkt.get("kkt_slot") or "тт.1"
                    addr = kkt.get("address")
                    ofd_date = kkt.get("ofd_expires_at")
                    fn_date = kkt.get("fn_expires_at")
                    mark_date = kkt.get("mark_expires_at")

                    if not ofd_date and not fn_date and not mark_date:
                        continue

                    lines: list[str] = []
                    header = f"<b>📍 {slot}</b>"
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
            print(e)
            kkt_block = "нет данных"

    # мягкая подсказка «наш клиент?»
    looks_like_our = await maybe_our_client(inn or None, phone or None)
    extra_done = bool(context.user_data.get("extra_verify_done"))
    # Если это наш клиент, но ещё не верифицирован и не отправлял доп. верификацию —
    # показываем кнопку «Пройти доп. верификацию»
    show_finish_verify = (not verified) and looks_like_our and not extra_done

    # купленные коды (ОФД / сервисы / ЭДО) — три блока подряд
    purchased_ofd_block = "—"
    purchased_service_block = "—"
    purchased_edo_block = "—"
    try:
        rows_codes = await get_user_codes(user_id=uid, limit=50, offset=0)
        now_dt = datetime.datetime.now()
        cutoff_dt = now_dt - datetime.timedelta(days=93)  # ~3 месяца

        buckets: dict[str, list[tuple[datetime.datetime, str]]] = {"ofd": [], "service": [], "edo": []}

        for r in (rows_codes or []):
            itype = (r.get("item_type") or r.get("code_type") or "").lower()

            kind = None
            # Поддержка новых ENUM типов: ofd_first, ofd_platform, ofd_other, service, edo
            # И старых форматов: ofd, ofd_ext:..., service_ext:..., edo_ext:...
            if itype in ("ofd", "ofd_first", "ofd_platform", "ofd_other") or itype.startswith("ofd_ext:"):
                kind = "ofd"
            elif itype == "service" or itype.startswith("service_ext:") or itype == "service_order" or itype.startswith("service_ord_ext:"):
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
            exp_dt = None
            date_part = ""
            sort_dt = datetime.datetime.max

            if exp:
                try:
                    exp_dt = exp
                    if isinstance(exp, str):
                        exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", ""))
                    if isinstance(exp_dt, datetime.date) and not isinstance(exp_dt, datetime.datetime):
                        exp_dt = datetime.datetime.combine(exp_dt, datetime.time.min)

                    # не показываем то, что истекло более ~3 месяцев назад
                    if exp_dt < cutoff_dt:
                        continue

                    days_left = (exp_dt.date() - now_dt.date()).days
                    date_str = exp_dt.strftime("%d.%m.%Y")
                    if days_left < 0:
                        date_part = f"до <b>{date_str}</b> ⛔ истёк"
                    else:
                        date_part = f"до <b>{date_str}</b> ({days_left} дн.)"
                    sort_dt = exp_dt
                except Exception:
                    date_part = ""
            else:
                date_part = "<b>бессрочно</b>"

            line = f"• {title2}: {date_part}"
            if code_val:
                line += f" — <code>{code_val}</code>"

            buckets[kind].append((sort_dt, line))

        def _mk(bucket: list[tuple[datetime.datetime, str]]) -> str:
            if not bucket:
                return "—"
            bucket.sort(key=lambda x: x[0])
            return "\n".join([x[1] for x in bucket[:6]])

        purchased_ofd_block = _mk(buckets["ofd"])
        purchased_service_block = _mk(buckets["service"])
        purchased_edo_block = _mk(buckets["edo"])
    except Exception as e:
        print(e)

    # блок лояльности
    loyalty_block = ""
    if verified:
        try:
            from services.loyalty_engine import is_loyalty_enabled, get_user_loyalty_summary
            from services.bot_loyalty_ui import format_loyalty_block
            from db.users import resolve_internal_user_id
            if await is_loyalty_enabled():
                internal_id = await resolve_internal_user_id(tg_user_id=int(uid))
                if internal_id:
                    summary = await get_user_loyalty_summary(internal_id)
                    loyalty_block = "\n\n<b>Программа лояльности</b>\n" + format_loyalty_block(summary, html=True)
        except Exception:
            pass

# текст профиля
    text = (
        f"<b>{(first_name + ' ' + last_name).strip() or '—'}</b>\n\n"
        f"<b>Телефон:</b> {phone or '—'}\n"
        f"<b>Email:</b> {email or '—'}\n"
        f"<b>ИНН:</b> {inn or '—'}\n"
        f"<b>Город:</b> {city or '—'}\n\n"
        f"<b>Сроки</b>\n"
        f"{kkt_block if verified else ('Появятся после подтверждения, если данные есть в системе.' if show_finish_verify else '—')}"
        f"\n\n<b>Купленные коды ОФД</b>\n{purchased_ofd_block}"
        f"\n\n<b>Купленные коды сервисов</b>\n{purchased_service_block}"
        f"\n\n<b>Купленные коды ЭДО</b>\n{purchased_edo_block}"
        f"{loyalty_block}"
    )

    kb = _profile_kb(verified, show_finish_verify)
    await send_or_edit(
        update,
        text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
        context=context
    )
    # q = getattr(update, "callback_query", None)
    # if q:
        # await q.answer()
        # await q.edit_message_text(
            # text,
            # parse_mode=ParseMode.HTML,
            # reply_markup=kb,
            # disable_web_page_preview=True,
        # )
    # else:
        # await update.effective_chat.send_message(
            # text,
            # parse_mode=ParseMode.HTML,
            # reply_markup=kb,
            # disable_web_page_preview=True,
        # )
    return STATE_IDLE


async def show_profile_ofd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = str(update.effective_user.id)
    inn = context.user_data.get("inn") or await get_user_inn(uid)

    # фрагмент внутри show_profile, когда u есть, но ИНН ещё не задан:
    if not inn:
        await q.edit_message_text(
            "Чтобы показать сроки ОФД, укажите ИНН.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏ Ввести ИНН", callback_data="edit_inn")],
                [InlineKeyboardButton("⬅ В профиль", callback_data="profile")],
            ])
        )
        return STATE_IDLE

    client = await get_client_by_inn(inn)
    if not client:
        await q.edit_message_text(
            f"По ИНН <code>{inn}</code> в нашей базе пока нет данных.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В профиль", callback_data="profile")]])
        )
        return STATE_IDLE

    slots = await get_client_ofd_slots(client["id"]) or []
    if not slots:
        await q.edit_message_text(
            f"По ИНН <code>{inn}</code> слоты ККТ не найдены.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В профиль", callback_data="profile")]])
        )
        return STATE_IDLE

    today = datetime.date.today()
    lines = [f"ИНН: <code>{inn}</code>", " "]
    for s in slots:
        try:
            d = _parse_date_only(s.get("ofd_expires_at"))
            if not d:
                raise ValueError('bad date')
            left = (d - today).days
            badge = _days_badge(left)
            lines.append(f"{badge} <b>{s['kkt_slot']}</b> — до <b>{d:%d.%m.%Y}</b> ({left} дн.)")
        except Exception as e:
            print(e)
            lines.append(f"• <b>{s['kkt_slot']}</b>: {s['ofd_expires_at']}")
    lines.append(" ")
    lines.append("Если данные отличаются от фактических — напишите нам, актуализируем.")

    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В профиль", callback_data="profile")]])
    )
    return STATE_IDLE


async def show_my_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    import datetime
    """Экран «Мои покупки» (только новая логика).

    - коды: ofd_first, ofd_platform, ofd_other, service, edo (показываем значение кода)
    - оборудование: equip (кода нет, показываем только позицию/заказ)
    """
    query = update.callback_query
    if query:
        await query.answer()
    if not _debounce_cb(update, context):
        return STATE_IDLE

    user_id = str(update.effective_user.id)

    # читаем смещение из callback_data: "my_codes" | "codes_prev:<off>" | "codes_next:<off>"
    data = (query.data or "my_codes")
    offset = 0
    if ":" in data:
        try:
            offset = max(0, int(data.split(":", 1)[1]))
        except Exception as e:
            print(e)
            offset = 0

    rows = await get_user_purchases(user_id=user_id, limit=PAGE_SIZE_CODES + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE_CODES
    rows = rows[:PAGE_SIZE_CODES]
    has_prev = offset > 0

    if not rows:
        text = "Пока покупок нет. После оплаты здесь появятся данные о заказах."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В профиль", callback_data="profile")]])
        await query.edit_message_text(text, reply_markup=kb)
        return STATE_IDLE

    lines: list[str] = []
    for i, r in enumerate(rows, start=offset + 1):
        # Типы строго как в БД/новой логике:
        #   коды: ofd_first, ofd_platform, ofd_other, service, edo
        #   оборудование: equip
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
                exp_s = ""

        # Оборудование — без кода; коды/услуги — с кодом
        if item_type == "equip":
            lines.append(f"{i}. {title} — заказ `{oid}` — {amt} ₽ — {issued}")
        else:
            lines.append(f"{i}. {title}: `{code}`{exp_s} — заказ `{oid}` — {amt} ₽ — {issued}")

    text = "📦 *Мои покупки*\n\n" + "\n".join(lines)

    # пагинация
    next_off = offset + PAGE_SIZE_CODES
    prev_off = max(0, offset - PAGE_SIZE_CODES)

    btns: list[InlineKeyboardButton] = []
    if has_prev:
        btns.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"codes_prev:{prev_off}"))
    if has_next:
        btns.append(InlineKeyboardButton("➡️ Далее", callback_data=f"codes_next:{next_off}"))

    kb_rows: list[list[InlineKeyboardButton]] = []
    if btns:
        kb_rows.append(btns)
    kb_rows.append([InlineKeyboardButton("🔙 В профиль", callback_data="profile")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode="Markdown")
    return STATE_IDLE


async def show_loyalty_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «История баллов» — показать последние операции."""
    q = update.callback_query
    if q:
        await q.answer()
    uid = str(update.effective_user.id)

    if not await is_user_verified(uid):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
            [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ])
        await send_or_edit(
            update,
            "⛔ Разделы программы лояльности доступны только после верификации.\n\n"
            "Подтвердите телефон и укажите ИНН.",
            reply_markup=kb,
            context=context,
        )
        return STATE_IDLE

    text = "Программа лояльности отключена."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ В профиль", callback_data="profile")]])

    try:
        from services.loyalty_engine import is_loyalty_enabled, get_user_loyalty_summary
        from services.bot_loyalty_ui import format_loyalty_history, format_loyalty_block
        from db.users import resolve_internal_user_id

        if await is_loyalty_enabled():
            internal_id = await resolve_internal_user_id(tg_user_id=int(uid))
            if internal_id:
                summary = await get_user_loyalty_summary(internal_id)
                header = format_loyalty_block(summary, html=True)
                history = format_loyalty_history(summary.get("history", []), html=True)
                text = f"<b>Программа лояльности</b>\n{header}\n\n{history}"
    except Exception:
        text = "Не удалось загрузить историю. Попробуйте позже."

    await send_or_edit(update, text, reply_markup=kb, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE


async def show_referral_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Реферальный прогресс» — прогресс QR-коллекции."""
    q = update.callback_query
    if q:
        await q.answer()
    uid = str(update.effective_user.id)

    if not await is_user_verified(uid):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
            [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ])
        await send_or_edit(
            update,
            "⛔ Разделы программы лояльности доступны только после верификации.\n\n"
            "Подтвердите телефон и укажите ИНН.",
            reply_markup=kb,
            context=context,
        )
        return STATE_IDLE

    text = "Программа лояльности отключена."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ В профиль", callback_data="profile")]])

    try:
        from services.loyalty_engine import is_loyalty_enabled
        from services.referral_engine import get_referral_progress
        from services.bot_loyalty_ui import format_referral_progress
        from db.users import resolve_internal_user_id

        if await is_loyalty_enabled():
            internal_id = await resolve_internal_user_id(tg_user_id=int(uid))
            if internal_id:
                progress = await get_referral_progress(internal_id)
                text = format_referral_progress(progress, html=True)
    except Exception:
        text = "Не удалось загрузить прогресс. Попробуйте позже."

    await send_or_edit(update, text, reply_markup=kb, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE


def _profile_kb(verified: bool, show_finish_verify: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if verified:
        rows.append([InlineKeyboardButton("Реферальный QR", callback_data="my_referral_qr")])
        rows.append([InlineKeyboardButton("⭐ История баллов", callback_data="loyalty_history")])
        rows.append([InlineKeyboardButton("🎯 Реферальный прогресс", callback_data="referral_progress")])

    # всегда показываем покупки (даже если их ещё нет — экран сам скажет "Пока покупок нет…")
    rows.append([InlineKeyboardButton("🧾 Мои заказы", callback_data="my_orders")])
    rows.append([InlineKeyboardButton("📦 Мои покупки", callback_data="my_codes")])
    rows.append([InlineKeyboardButton("✏️ Редактировать профиль", callback_data="edit_profile")])

    # мягкая кнопка "Завершить верификацию" — только если это наш клиент и ещё не подтверждён
    if (not verified) and show_finish_verify:
        rows.append([InlineKeyboardButton("🔐 Пройти доп. верификацию", callback_data="profile_finish_verify")])

    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")])

    return InlineKeyboardMarkup(rows)


def _days_badge(days_left: int) -> str:
    if days_left < 0: return "⛔ истёк"
    if days_left <= 1: return "1 день"
    if days_left <= 7: return f"{days_left} дн."
    if days_left <= 30: return f"{days_left} дн."
    return f"{days_left} дн."


# ===== Редактирование профиля =====
async def edit_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = str(update.effective_user.id)
    await hydrate_user_cache(context, uid)

    # Если нет согласия — сначала просим принять условия ОПД
    if not (await _has_consent(uid)):
        await show_consent_once(update, context)
        return S1_IDENT

    # Удаляем экран профиля перед отправкой меню редактирования
    if q and q.message:
        try:
            await q.message.delete()
        except Exception:
            pass

    # Отправляем НОВОЕ сообщение с меню редактирования
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Имя", callback_data="edit_firstname"),
         InlineKeyboardButton("Фамилия", callback_data="edit_lastname")],
        [InlineKeyboardButton("Телефон", callback_data="edit_phone"),
         InlineKeyboardButton("Email", callback_data="edit_email")],
        [InlineKeyboardButton("ИНН", callback_data="edit_inn"),
         InlineKeyboardButton("Город", callback_data="edit_city")],
        [InlineKeyboardButton("⬅️ Назад в профиль", callback_data="profile")]
    ])
    msg = await update.effective_chat.send_message("Что изменить?", reply_markup=kb)
    _track_prompt_mid(context, msg.message_id)
    return STATE_EDIT_MENU


# --- Prompts
async def edit_firstname_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    msg = await update.effective_chat.send_message("Введите новое имя:")
    _track_prompt_mid(context, msg.message_id)
    return STATE_EDIT_FIRSTNAME


async def edit_lastname_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    msg = await update.effective_chat.send_message("Введите новую фамилию:")
    _track_prompt_mid(context, msg.message_id)
    return STATE_EDIT_LASTNAME


async def edit_phone_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📲 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже"
    )
    msg = await update.effective_chat.send_message("Поделитесь номером телефона:", reply_markup=kb)
    _track_prompt_mid(context, msg.message_id)
    return STATE_EDIT_PHONE


async def edit_email_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    msg = await update.effective_chat.send_message("Введите новый e-mail:")
    _track_prompt_mid(context, msg.message_id)
    return STATE_EDIT_EMAIL


async def edit_inn_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показываем запрос на ввод ИНН из любого места (в т.ч. из профиля)."""
    q = update.callback_query
    if q:
        await q.answer()
    msg = await update.effective_chat.send_message(
        "Введите ИНН (10 или 12 цифр):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ В профиль", callback_data="profile")]
        ]))
    _track_prompt_mid(context, msg.message_id)
    return STATE_EDIT_INN


async def edit_city_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    msg = await update.effective_chat.send_message("Введите новый город:")
    _track_prompt_mid(context, msg.message_id)
    return STATE_EDIT_CITY


def _track_prompt_mid(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> None:
    """Запоминаем message_id prompt-сообщения для последующего удаления."""
    key = "_profile_prompt_mids"
    arr = context.user_data.setdefault(key, [])
    arr.append(int(message_id))
    if len(arr) > 20:
        context.user_data[key] = arr[-20:]


async def _cleanup_profile_prompts(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Удаляем все запомненные prompt-сообщения профиля."""
    key = "_profile_prompt_mids"
    mids = context.user_data.pop(key, []) or []
    for mid in mids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


# --- Loyalty: бонус за заполнение поля профиля ---
async def _try_award_profile_bonus(tg_user_id: str, field_name: str) -> None:
    """Best-effort начисление баллов за заполнение поля профиля."""
    try:
        from services.loyalty_engine import is_loyalty_enabled, award_profile_completion_bonus
        from db.users import resolve_internal_user_id

        if not await is_loyalty_enabled():
            return
        internal_id = await resolve_internal_user_id(tg_user_id=int(tg_user_id))
        if internal_id:
            await award_profile_completion_bonus(internal_id, field_name=field_name)
    except Exception:
        import logging
        logging.getLogger(__name__).debug(
            "Profile bonus failed for tg_user=%s field=%s — не критично",
            tg_user_id, field_name, exc_info=True,
        )


# --- Saves
async def save_firstname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_in = update.message
    if msg_in:
        await _ui.track_user_input(update, context)
        await _cleanup_profile_prompts(context, int(msg_in.chat_id))

    val = (msg_in.text or "").strip()
    uid = str(update.effective_user.id)
    await upsert_user(uid, first_name=val)
    context.user_data["first_name"] = val
    await _try_award_profile_bonus(uid, "first_name")
    # Сразу возвращаем в профиль — не используем notice чтобы не создавать временные сообщения
    return await show_profile(update, context)


async def save_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_in = update.message
    if msg_in:
        await _ui.track_user_input(update, context)
        await _cleanup_profile_prompts(context, int(msg_in.chat_id))

    val = (msg_in.text or "").strip()
    uid = str(update.effective_user.id)
    await upsert_user(uid, last_name=val)
    context.user_data["last_name"] = val
    await _try_award_profile_bonus(uid, "last_name")
    return await show_profile(update, context)


async def save_phone_from_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # входящее сообщение с контактом — тоже ПДн
    if update.effective_message:
        schedule_pii_delete(context, update.effective_message)

    if not update.effective_message.contact:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📲 Поделиться номером", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        msg = await update.effective_chat.send_message(
            "Телефон вводить вручную нельзя. Пожалуйста, поделитесь номером через Telegram.",
            reply_markup=kb
        )
        return STATE_EDIT_PHONE

    phone = _normalize_phone(update.effective_message.contact.phone_number)
    if not phone:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📲 Поделиться номером", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        msg = await update.effective_chat.send_message(
            "Не удалось распознать номер. Попробуйте ещё раз кнопкой ниже.",
            reply_markup=kb
        )
        return STATE_EDIT_PHONE

    uid = str(update.effective_user.id)
    await upsert_user(uid, phone=phone)
    context.user_data["phone"] = phone
    context.user_data["phone_confirmed"] = True
    await _try_award_profile_bonus(uid, "phone")

    msg2 = await update.effective_chat.send_message(
        f"✅ Телефон подтверждён через Telegram: <code>{phone}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )
    schedule_pii_delete(context, msg2)

    if context.user_data.get("inn"):
        return await verify_and_finish(update, context)

    return await show_profile(update, context)


async def reject_manual_phone_in_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📲 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.effective_chat.send_message(
        "Чтобы изменить номер, нажмите «📲 Поделиться номером». Вручной ввод отключён.",
        reply_markup=kb
    )
    return STATE_EDIT_PHONE


async def save_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_in = update.message
    if msg_in:
        await _ui.track_user_input(update, context)
        await _cleanup_profile_prompts(context, int(msg_in.chat_id))

    email = (msg_in.text or "").strip()
    if not EMAIL_RE.match(email):
        msg = await update.message.reply_text("❌ Некорректный e-mail. Пример: user@example.com")
        return STATE_EDIT_EMAIL
    uid = str(update.effective_user.id)
    await upsert_user(uid, email=email)
    context.user_data["email"] = email
    await _try_award_profile_bonus(uid, "email")

    # Clean up the email request message if from checkout
    email_req_mid = context.user_data.pop("_email_request_mid", None)
    if email_req_mid:
        try:
            await context.bot.delete_message(chat_id=int(update.effective_chat.id), message_id=email_req_mid)
        except Exception:
            pass

    if context.user_data.pop("co_mode", None) == "checkout":
        return await checkout(update, context)
    return await show_profile(update, context)


async def save_inn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_in = update.message
    if msg_in:
        await _ui.track_user_input(update, context)
        await _cleanup_profile_prompts(context, int(msg_in.chat_id))

    inn = _normalize_inn(msg_in.text or "")
    if not inn:
        msg = await update.message.reply_text(
            "❌ Некорректный ИНН. Введите 10 или 12 цифр.\n\n"
            "Например: 7707083893 или 500100732259"
        )
        return STATE_EDIT_INN

    uid = str(update.effective_user.id)
    await set_user_inn(uid, inn)
    context.user_data["inn"] = inn
    context.user_data.pop("skipped_inn", None)
    await _try_award_profile_bonus(uid, "inn")

    if context.user_data.pop("co_mode", None) == "checkout":
        return await checkout(update, context)
    if context.user_data.get("phone_confirmed"):
        return await verify_and_finish(update, context)

    return await show_profile(update, context)


async def save_kkt_factory_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_in = update.message
    if msg_in:
        schedule_pii_delete(context, msg_in)
    
    kkt_number = msg_in.text
    if not re.fullmatch(r"\d+", kkt_number):
        msg = await update.message.reply_text(
            "❌ Некорректный Номер. Введите ещё раз"
        )
        return STATE_CO_KKT_FACTORY_NUMBER

    uid = str(update.effective_user.id)
    await set_user_kkt_factory_number(uid, kkt_number)

    mode = context.user_data.pop("co_mode", None)
    if mode == "checkout":
        await update.message.reply_text("✅ Заводской номер ККТ сохранён.")
        return await checkout(update, context)

    if mode == "service_orders_pick":
        await update.message.reply_text("✅ Заводской номер ККТ сохранён. Теперь можно перейти к оплате в корзине.")
        return await show_cart(update, context)

    await update.message.reply_text(
        "✅ Заводской номер ККТ сохранён.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ В профиль", callback_data="profile")]])
    )

    return STATE_IDLE

async def renew_inn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            await send_or_edit(
                update,
                "Введите ИНН:",
                reply_markup=None,
                parse_mode=ParseMode.HTML,
            )
            return STATE_CO_INN

async def renew_kkt_factory_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            await send_or_edit(
                update,
                "Введите заводской номер ККТ:",
                reply_markup=None,
                parse_mode=ParseMode.HTML,
            )
            return STATE_CO_KKT_FACTORY_NUMBER

async def save_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_in = update.message
    if msg_in:
        await _ui.track_user_input(update, context)
        await _cleanup_profile_prompts(context, int(msg_in.chat_id))

    city = (msg_in.text or "").strip()
    if not city:
        await _ui.render_notice(update, context, "Укажите город:")
        return STATE_CO_CITY
    uid = str(update.effective_user.id)
    try:
        await set_user_city(uid, city)
    except Exception as e:
        print(e)
        pass
    context.user_data["city"] = city
    await _try_award_profile_bonus(uid, "city")

    # 👇 если пришли из checkout — продолжаем его
    if context.user_data.pop("co_mode", None) == "checkout":
        return await checkout(update, context)

    return await show_profile(update, context)


async def save_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.bot_catalog import checkout
    msg_in = update.message
    if msg_in:
        await _ui.track_user_input(update, context)

    address = (msg_in.text or "").strip()
    if not address:
        await update.message.reply_text("Введите адрес доставки:")
        return STATE_CO_ADDRESS

    uid = str(update.effective_user.id)
    await set_user_address(uid, address)
    context.user_data["_co_address"] = address
    context.user_data["_co_address_decided"] = True

    context.user_data.pop("co_mode", None)
    return await checkout(update, context)


async def handle_co_addr_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь выбрал использовать сохранённый адрес."""
    from bot.bot_catalog import checkout
    q = update.callback_query
    if q:
        await q.answer()
    user = await get_user(str(update.effective_user.id)) or {}
    saved = (user.get("address") or "").strip()
    context.user_data["_co_address"] = saved or None
    context.user_data["_co_address_decided"] = True
    context.user_data.pop("co_mode", None)
    return await checkout(update, context)


async def handle_co_addr_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь хочет ввести новый адрес."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    q = update.callback_query
    if q:
        await q.answer()
    await update.effective_chat.send_message(
        "Введите новый адрес доставки:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В корзину", callback_data="cart")]]),
        parse_mode=ParseMode.HTML,
    )
    return STATE_CO_ADDRESS


# ===== Анкета "Заявка / Оформление" + Django CRM =====
async def application_form_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()

    uid = str(update.effective_user.id)
    await hydrate_user_cache(context, uid)

    # 1) Без согласия на ОПД — сначала показываем соглашение
    if not (await _has_consent(uid)):
        return await start_registration(update, context)

    # 2) Нужен подтверждённый телефон (через контакт Telegram / из БД)
    if not context.user_data.get("phone_confirmed"):
        msg = await update.effective_chat.send_message(
            "Для отправки заявки нам нужен подтверждённый номер телефона.\n\n"
            "Пожалуйста, поделитесь номером через Telegram:",
            reply_markup=kb_request_contact()
        )
        return S2_WAIT_CONTACT

    # 3) Всё ок — начинаем анкету
    context.user_data["form"] = {}
    await send_or_edit(
        update,
        "📝 Анкета клиента\n\nВведите *Название торговой точки*:",
        back_menu_kb(),
        parse_mode="Markdown"
    )
    return STATE_FORM_TP


async def form_tp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["tp_name"] = (update.message.text or "").strip()
    if update.message:
        schedule_pii_delete(context, update.message)
    await asyncio.sleep(3)
    msg = await update.message.reply_text("Адрес торговой точки:", reply_markup=back_menu_kb())
    return STATE_FORM_ADDR


async def form_addr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["form"]["tp_addr"] = (update.message.text or "").strip()
    if update.message:
        schedule_pii_delete(context, update.message)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Продовольственная розница", callback_data="act_food")],
        [InlineKeyboardButton("Непродовольственная розница", callback_data="act_nonfood")],
        [InlineKeyboardButton("Услуги", callback_data="act_services")],
        [InlineKeyboardButton("Оптовая торговля", callback_data="act_wholesale")],
        [InlineKeyboardButton("Маркетплейсы", callback_data="act_marketplaces")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await asyncio.sleep(3)
    msg = await update.message.reply_text("Выберите *вид деятельности*:", reply_markup=kb, parse_mode="Markdown")
    return STATE_FORM_ACT


async def form_act_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    act_map = {
        "act_food": "продовольственная розница",
        "act_nonfood": "непродовольственная розница",
        "act_services": "услуги",
        "act_wholesale": "оптовая торговля",
        "act_marketplaces": "маркетплейсы",
    }
    context.user_data["form"]["activity"] = act_map.get(update.callback_query.data, "")
    await update.callback_query.edit_message_text(
        "Опишите *основу ассортимента*:",
        parse_mode="Markdown",
        reply_markup=back_menu_kb()
    )
    return STATE_FORM_ASSORT


async def form_assort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # сохраняем основу ассортимента
    context.user_data.setdefault("form", {})
    context.user_data["form"]["assort"] = (update.message.text or "").strip()
    if update.message:
        schedule_pii_delete(context, update.message)

    # инициализируем список доп. сервисов
    context.user_data["form"]["services"] = context.user_data["form"].get("services") or []

    kb = _services_kb(context.user_data["form"]["services"])
    await asyncio.sleep(3)
    msg = await update.message.reply_text(
        "Выберите дополнительные сервисы (можно несколько):\n"
        "Нажимайте по пунктам, затем «Готово».",
        reply_markup=kb,
    )
    return STATE_FORM_SERVICES


def _services_kb(selected: list[str] | None = None) -> InlineKeyboardMarkup:
    """
    Клавиатура для выбора доп. сервисов (множественный выбор).
    """
    selected = selected or []
    options = [
        ("Честный знак", "svc_chz"),
        ("Меркурий", "svc_merk"),
        ("ЕГАИС", "svc_ega"),
        ("Статистика", "svc_stat"),
    ]

    rows = []
    for label, cb in options:
        prefix = "✅ " if label in selected else ""
        rows.append([InlineKeyboardButton(prefix + label, callback_data=cb)])

    rows.append([InlineKeyboardButton("Готово", callback_data="svc_done")])
    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")])
    return InlineKeyboardMarkup(rows)


async def form_services_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    data = update.callback_query.data

    form = context.user_data.setdefault("form", {})
    services: list[str] = form.get("services") or []

    label_map = {
        "svc_chz": "Честный знак",
        "svc_merk": "Меркурий",
        "svc_ega": "ЕГАИС",
        "svc_stat": "Статистика",
    }

    # если нажато "Готово" — переходим к выбору СНО
    if data == "svc_done":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("УСН 6%", callback_data="sno_usn6")],
            [InlineKeyboardButton("УСН Д-Р 15%", callback_data="sno_usn15")],
            [InlineKeyboardButton("Патент", callback_data="sno_patent")],
            [InlineKeyboardButton("ОСНО", callback_data="sno_osn")],
            [InlineKeyboardButton("Самозанятый", callback_data="sno_self")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ])
        await update.callback_query.edit_message_text(
            "Укажите систему налогообложения (СНО):",
            reply_markup=kb,
        )
        return STATE_FORM_SNO

    # иначе — это один из сервисов, переключаем его состояние (вкл/выкл)
    label = label_map.get(data)
    if not label:
        return STATE_FORM_SERVICES

    if label in services:
        services.remove(label)
    else:
        services.append(label)

    form["services"] = services

    kb = _services_kb(services)
    chosen_text = ", ".join(services) if services else "ничего не выбрано"
    await update.callback_query.edit_message_text(
        "Выберите дополнительные сервисы (можно несколько):\n"
        f"Сейчас отмечено: {chosen_text}",
        reply_markup=kb,
    )
    return STATE_FORM_SERVICES


async def form_sno_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    data = update.callback_query.data

    sno_map = {
        "sno_usn6": "УСН 6%",
        "sno_usn15": "УСН Д-Р 15%",
        "sno_patent": "Патент",
        "sno_osn": "ОСНО",
        "sno_self": "Самозанятый",
    }

    form = context.user_data.setdefault("form", {})
    form["sno"] = sno_map.get(data, "")

    # после СНО — старый шаг про маркировку
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Да", callback_data="marked_yes"),
         InlineKeyboardButton("Нет", callback_data="marked_no")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await update.callback_query.edit_message_text(
        "Продаёте маркированный товар?",
        reply_markup=kb,
    )
    return STATE_FORM_MARKED


async def form_marked_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["form"]["marked"] = "да" if update.callback_query.data.endswith("yes") else "нет"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ФН 15 мес", callback_data="fn_15_term"),
         InlineKeyboardButton("ФН 36 мес", callback_data="fn_36_term")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await update.callback_query.edit_message_text("Срок действия ФН:", reply_markup=kb)
    return STATE_FORM_FNTERM


async def form_fnterm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["form"]["fn_term"] = "15 мес" if "15" in update.callback_query.data else "36 мес"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Любой", callback_data="ofd_any")],
        [InlineKeyboardButton("Первый ОФД", callback_data="ofd_first_opt")],
        [InlineKeyboardButton("Платформа ОФД", callback_data="ofd_platform_opt")],
    ])
    await update.callback_query.edit_message_text("ОФД:", reply_markup=kb)
    return STATE_FORM_OFD


async def form_ofd_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    m = update.callback_query.data
    ofd = "Любой" if "any" in m else ("Первый ОФД" if "first" in m else "Платформа ОФД")
    context.user_data["form"]["ofd"] = ofd
    await update.callback_query.edit_message_text(
        "В каких банках у вас есть открытый расчётный счёт?",
        reply_markup=back_menu_kb()
    )
    return STATE_FORM_BANKS


async def form_banks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # сохраняем последний ответ анкеты
    context.user_data.setdefault("form", {})
    context.user_data["form"]["banks"] = (update.message.text or "").strip()
    if update.message:
        schedule_pii_delete(context, update.message)
    f = context.user_data["form"]

    # данные профиля для контакта в CRM
    name = context.user_data.get("first_name") or (update.effective_user.first_name or "Клиент")
    phone = (context.user_data.get("phone") or "").strip()
    email = (context.user_data.get("email") or "").strip()
    inn = (context.user_data.get("inn") or "").strip()
    city = (context.user_data.get("city") or "").strip()
    uname = update.effective_user.username or ""

    # собираем подробную заметку (включаем профиль + всю анкету)
    note_lines = [
        "Заявка/Оформление",
        "====================",
        "Профиль клиента:",
        f"• Имя: {name or '—'}",
        f"• Телефон: {phone or '—'}",
        f"• E-mail: {email or '—'}",
        f"• ИНН: {inn or '—'}",
        f"• Город: {city or '—'}",
        f"• Telegram: @{uname}" if uname else "• Telegram: —",
        "",
        "Анкета:",
        f"• Название ТТ: {f.get('tp_name') or '—'}",
        f"• Адрес ТТ: {f.get('tp_addr') or '—'}",
        f"• Вид деятельности: {f.get('activity') or '—'}",
        f"• Основа ассортимента: {f.get('assort') or '—'}",
        f"• Маркировка (да/нет): {f.get('marked') or '—'}",
        f"• Срок действия ФН: {f.get('fn_term') or '—'}",
        f"• ОФД: {f.get('ofd') or '—'}",
        f"• Банки с Р/С: {f.get('banks') or '—'}",
    ]
    note_text = "\n".join(note_lines)

    # осмысленное имя сделки по анкете
    lead_title_parts = ["Заявка оформления"]
    if f.get("tp_name"):
        lead_title_parts.append(f"— {f.get('tp_name')}")
    lead_title = " ".join(lead_title_parts)

    await asyncio.sleep(3)
    if (await is_specialist_user(update, context)):
        await update.effective_chat.send_message(
            "🧑‍🔧 Режим навигации специалиста: мы не отправляем заявку в CRM."
        )
        from bot_agent_chat import agent_entry
        return await agent_entry(update, context)
    try:
        # Отправляем ВСЮ анкету + профиль в Django CRM
        await push_application_to_crm_async(context, context.user_data["form"])
    except Exception as e:
        await update.message.reply_text(
            f"✅ Заявка принята, но не удалось отправить в CRM: {e}"
        )
        return await show_main_menu(update, context, "Чем ещё помочь?")

    await update.message.reply_text("✅ Заявка отправлена в CRM. Спасибо! Мы свяжемся с вами.")
    return await show_main_menu(update, context, "Чем ещё помочь?")


async def push_application_to_crm_async(context, form: dict):
    """
    Отправляет заявку/оформление в Django CRM.
    """
    ud = context.user_data
    first = (ud.get("first_name") or "").strip()
    last = (ud.get("last_name") or "").strip()
    phone = (ud.get("phone") or "").strip()
    email = (ud.get("email") or "").strip()
    inn = (ud.get("inn") or "").strip()
    city = (ud.get("city") or "").strip()
    uname = ud.get("tg_username") or ""

    user_data = {
        "first_name": first,
        "last_name": last,
        "phone": phone,
        "email": email,
        "inn": inn,
        "city": city,
        "tg_username": uname,
    }

    await push_application_to_crm(form, source="telegram", user_data=user_data)


# ===== Навигация / refresh / сброс / fallback =====

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
        if not _debounce_cb(update, context):
            return STATE_IDLE

    uid_int = int(update.effective_user.id)
    uid = str(uid_int)
    try:
        await clear_user_notices_tg(
            tg_bot=context.application.bot,
            tg_chat_id=uid_int,
            user_id=uid_int,
        )
    except Exception:
        logger.debug("best-effort back_to_main notice cleanup failed for uid=%s", uid_int, exc_info=True)

    # ✅ Специалистам (кроме директора) запрещаем выходить в клиентское меню
    if await is_specialist_user(update, context):
        try:
            spec = await get_specialist_by_tg(tg_user_id=uid_int, tg_username=update.effective_user.username or "")
        except Exception:
            spec = None

        role = str((spec or {}).get("role") or "specialist")
        if role != "director":
            from bot_agent_chat import agent_entry  # локальный импорт, чтобы не ловить циклы
            return await agent_entry(update, context)

    if not (await _has_consent(uid)):
        return await start_registration(update, context)

    return await show_main_menu(update, context)


async def text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Ловим любой текст, который НЕ обработал ConversationHandler.
    Показываем меню + нижнюю клавиатуру.
    """
    msg = update.message
    if not msg or not msg.text:
        return STATE_IDLE

    raw = msg.text.strip()
    uid = str(update.effective_user.id)
    await hydrate_user_cache(context, uid)

    first_name = (context.user_data.get("first_name")
                  or update.effective_user.first_name
                  or "Коллега").strip()
    lower = raw.lower()

    # что именно хотел нажать пользователь
    if "меню" in lower:
        await show_main_menu(update, context, f"{first_name}, чем могу помочь?")
    elif "услуг" in lower or "тариф" in lower:
        await show_services(update, context)
    elif "профил" in lower or "подписк" in lower:
        await show_profile(update, context)
    else:
        # обычный произвольный текст — просто покажем главное меню
        await show_main_menu(update, context, f"{first_name}, чем могу помочь?")

    return STATE_IDLE


async def reset_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await reset_user_profile(uid)  # мягкий сброс профиля
    context.user_data.clear()
    await update.message.reply_text(
        "Профиль очищен. Нажмите «Начать регистрацию».",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Начать регистрацию", callback_data="register")]])
    )



async def wipe_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Soft wipe: keep user/client record but remove Telegram identity."""
    uid = str(update.effective_user.id)
    stats = await wipe_user_telegram_identity(uid)
    context.user_data.clear()
    await update.message.reply_text(
        f"Готово. Telegram-профиль отвязан (обновлено записей: {stats.get('users_updated', 0)}).\n"
        "Нажмите «Начать регистрацию».",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Начать регистрацию", callback_data="register")]])
    )

async def delete_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hard delete: delete user, orders and return codes to pool."""
    uid = str(update.effective_user.id)
    stats = await wipe_user_all(uid)
    context.user_data.clear()
    await update.message.reply_text(
        f"Готово. Освобождено кодов: {stats.get('codes_reset', 0)}, "
        f"удалено заказов: {stats.get('orders_deleted', 0)}, "
        f"удалено пользователей: {stats.get('users_deleted', 0)}.\n"
        "Нажмите «Начать регистрацию».",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Начать регистрацию", callback_data="register")]])
    )

REFERRAL_CODE_RE = re.compile(r"^\d{4}$")


async def referral_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Секретные реф-коды: пользователь просто пишет 4 цифры (например 1111),
    бот фиксирует источник и отвечает "Мы зафиксировали".
    Работает в любой момент, потому что будет повешено выше ConversationHandler.
    """
    msg = update.effective_message
    if not msg or not msg.text:
        return

    # чтобы не ловить случайные фразы
    raw = msg.text.strip()
    logger.info("[REF-TG] message referral trigger check: tg_user_id=%s text=%r", update.effective_user.id, raw)

    # ограничиваемся 4 цифрами (как ты хотел: 1111, 2222)
    if not REFERRAL_CODE_RE.fullmatch(raw):
        return

    # если такого кода нет — ничего не делаем (чтобы не палить механику и не мешать диалогу)
    rc = await get_referral_code(raw)
    if not rc:
        logger.info("[REF-TG] message referral code not found: tg_user_id=%s code=%r", update.effective_user.id, raw)
        return

    uid = update.effective_user.id
    logger.info("[REF-TG] message referral apply start: tg_user_id=%s code=%r", uid, raw)
    res = await apply_referral_code(uid, raw_code=raw, raw_text=raw, source_platform="telegram")
    logger.info("[REF-TG] message referral apply result: tg_user_id=%s code=%r result=%s", uid, raw, res)

    if res.get("ok") and res.get("status") == "applied":
        await msg.reply_text("Мы зафиксировали ✅ Спасибо!")
    elif res.get("ok") and res.get("status") == "already_set":
        await msg.reply_text("Код уже был зафиксирован ранее ✅")
    else:
        await msg.reply_text("Не удалось зафиксировать код. Попробуйте позже.")

    # важно: останавливаем дальнейшую обработку, чтобы ConversationHandler не съел '1111' как имя/город и т.п.
    raise ApplicationHandlerStop
