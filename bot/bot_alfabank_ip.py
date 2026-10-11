"""Заявка на оформление ИП через API Альфа-Банка.

Точка входа — кнопка «🏢 Открыть ИП» в корне раздела «Услуги».

Поведение:
* Если у пользователя уже есть отправленная заявка — запрашиваем у API
  её актуальный статус и отдаём сообщение «рассматривается / принята /
  отклонена».
* Иначе — запускаем пошаговый мастер: 6 шагов (имя, фамилия, телефон,
  email, ИНН, город). Каждый шаг предлагает использовать значение из
  профиля или ввести новое. После 6-го шага — экран подтверждения.

Стратегия city_code:
    SDK ``alfabank_partner_api`` отдаёт справочник городов через
    ``client.dictionaries.cities()`` (вспомогательный запрос). Используем
    его, чтобы найти city_code по названию города из профиля. Если не
    нашли — отправляем введённое пользователем название как ``city_code``
    (бэкенд Альфа-Банка вернёт ошибку валидации — её мы аккуратно
    обрабатываем и просим уточнить город).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

# Сделать SDK импортируемым: модуль лежит в alfabank_partner_api/src/
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_SRC = _REPO_ROOT / "alfabank_partner_api" / "src"
if str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))

from alfabank_partner_api import (  # noqa: E402
    AlfaBankAPIError,
    AlfaBankAsyncClient,
    AlfaBankNetworkError,
    ClientConfig,
    ContactInfo,
    DEMO_API_KEY,
    DictionaryCache,
    LeadCreateRequest,
    LeadState,
    OrganizationInfo,
    ProductCode,
    ProductItem,
    RequestInfo,
    ServerError,
    ServiceUnavailableError,
    UnacceptableCompanyError,
)

from bot_common import (  # noqa: E402
    STATE_IDLE,
    send_or_edit,
)
from message_cleanup import schedule_delete_message_tg  # noqa: E402
from db import (  # noqa: E402
    create_application,
    get_active_application,
    get_user,
    set_user_city,
    update_application_status,
    upsert_user,
)
from db.users import resolve_internal_user_id  # noqa: E402

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_DIGITS_RE = re.compile(r"\D+")

# ── Callback constants ─────────────────────────────────────────────────
CB_ENTRY = "svc_ip"
CB_SUBMIT = "ip_submit"
CB_REFRESH_STATUS = "ip_refresh_status"

# Wizard callbacks
CB_WIZ_CONFIRM = "ip_wiz_confirm"
CB_WIZ_CHANGE = "ip_wiz_change"
CB_WIZ_CANCEL = "ip_wiz_cancel"
CB_WIZ_CITY_RETRY = "ip_wiz_city_retry"

# ── user_data keys ─────────────────────────────────────────────────────
_PENDING_EDIT_KEY = "_ip_pending_edit"   # поле, ожидающее ввода
_WIZ_STEP_KEY = "_ip_wiz_step"           # int 0-5
_WIZ_DATA_KEY = "_ip_wiz_data"           # dict of confirmed values
_WIZ_MSG_KEY = "_ip_wiz_msg_id"          # message_id wizard message

# Кэш справочника городов (живёт пока процесс жив).
_CITIES_CACHE = DictionaryCache(ttl=24 * 60 * 60)

# ── Wizard field order ─────────────────────────────────────────────────
_WIZ_FIELDS = ["first_name", "last_name", "phone", "email", "inn", "city"]

_WIZ_LABELS = {
    "first_name": "Имя",
    "last_name": "Фамилия",
    "phone": "Телефон",
    "email": "E-mail",
    "inn": "ИНН",
    "city": "Город",
}

_WIZ_PROMPTS = {
    "first_name": "Введите имя:",
    "last_name": "Введите фамилию:",
    "email": "Введите e-mail (например, name@example.com):",
    "inn": "Введите ИНН (10 или 12 цифр):",
    "city": "Введите город (например: Москва):",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

def _alfabank_config() -> ClientConfig:
    api_key = os.getenv("ALFABANK_API_KEY") or DEMO_API_KEY
    base_url = os.getenv("ALFABANK_BASE_URL") or "https://partner.alfabank.ru/public-api/v2"
    return ClientConfig(api_key=api_key, base_url=base_url)


def _digits(s: str | None) -> str:
    return _DIGITS_RE.sub("", s or "")


def _phone_for_alfabank(phone: str | None) -> str | None:
    """Привести телефон из профиля (``+7XXXXXXXXXX``) к виду ``7XXXXXXXXXX``."""
    d = _digits(phone)
    if len(d) == 11 and d.startswith("7"):
        return d
    if len(d) == 10:
        return "7" + d
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


def _normalize_inn(raw: str | None) -> str | None:
    d = _digits(raw)
    if len(d) not in (10, 12):
        return None
    if not _validate_inn_checksum(d):
        return None
    return d


def _format_phone_human(phone: str | None) -> str:
    if not phone:
        return "—"
    d = _digits(phone)
    if len(d) == 11 and d.startswith("7"):
        return f"+7 {d[1:4]} {d[4:7]} {d[7:9]} {d[9:11]}"
    return phone


def _full_name(user: dict[str, Any]) -> str:
    parts = [(user.get("first_name") or "").strip(), (user.get("last_name") or "").strip()]
    return " ".join(p for p in parts if p)


def _get_profile_value(user: dict[str, Any], field: str) -> str | None:
    """Return validated profile value for field, or None if absent/invalid."""
    if field == "phone":
        raw = user.get("phone")
        return raw if _phone_for_alfabank(raw) else None
    if field == "email":
        raw = (user.get("email") or "").strip()
        return raw if EMAIL_RE.match(raw) else None
    if field == "inn":
        return _normalize_inn(user.get("inn"))
    # first_name, last_name, city
    raw = (user.get(field) or "").strip()
    return raw if raw else None


def _format_for_display(field: str, value: str) -> str:
    """Format a field value for human-readable display."""
    if field == "phone":
        return _format_phone_human(value)
    return value


def _wizard_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Отмена", callback_data=CB_WIZ_CANCEL)],
    ])


async def _wiz_send_or_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    kb: InlineKeyboardMarkup,
) -> None:
    """Try to edit existing wizard message; fall back to sending a new one."""
    chat = update.effective_chat
    wiz_mid = context.user_data.get(_WIZ_MSG_KEY)

    # 1) Try editing the callback query message directly
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.edit_text(text=text, reply_markup=kb)
            context.user_data[_WIZ_MSG_KEY] = update.callback_query.message.message_id
            return
        except Exception:
            pass

    # 2) Try editing via stored message_id
    if wiz_mid and chat:
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id,
                message_id=int(wiz_mid),
                text=text,
                reply_markup=kb,
            )
            return
        except Exception:
            pass

    # 3) Send new message
    if chat:
        msg = await chat.send_message(text=text, reply_markup=kb)
        context.user_data[_WIZ_MSG_KEY] = msg.message_id


async def _show_wizard_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    step: int,
    user: dict[str, Any] | None = None,
) -> int:
    """Show wizard step for the given step index (0-5)."""
    if step >= 6:
        return await _wizard_confirm_screen(update, context)

    context.user_data[_WIZ_STEP_KEY] = step
    context.user_data.pop(_PENDING_EDIT_KEY, None)

    field = _WIZ_FIELDS[step]
    label = _WIZ_LABELS[field]

    if user is None:
        tg_uid = str(update.effective_user.id)
        user = await get_user(tg_uid) or {}

    existing = _get_profile_value(user, field)

    if field == "phone":
        if existing:
            text = (
                f"Шаг {step + 1}/6 — {label}\n\n"
                f"В профиле: {_format_for_display(field, existing)}\n\n"
                "Использовать этот номер?"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Использовать", callback_data=CB_WIZ_CONFIRM)],
                [InlineKeyboardButton("🚫 Отмена", callback_data=CB_WIZ_CANCEL)],
            ])
        else:
            text = (
                f"Шаг {step + 1}/6 — {label}\n\n"
                "Телефон не найден в профиле.\n"
                "Чтобы добавить номер, откройте Мой профиль и привяжите телефон "
                "через кнопку «Поделиться контактом»."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
                [InlineKeyboardButton("🚫 Отмена", callback_data=CB_WIZ_CANCEL)],
            ])
        await _wiz_send_or_edit(update, context, text, kb)
        return STATE_IDLE

    # Non-phone field
    if existing:
        text = (
            f"Шаг {step + 1}/6 — {label}\n\n"
            f"В профиле: {_format_for_display(field, existing)}\n\n"
            "Использовать это значение?"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Использовать", callback_data=CB_WIZ_CONFIRM),
             InlineKeyboardButton("✏️ Ввести другое", callback_data=CB_WIZ_CHANGE)],
            [InlineKeyboardButton("🚫 Отмена", callback_data=CB_WIZ_CANCEL)],
        ])
        await _wiz_send_or_edit(update, context, text, kb)
        return STATE_IDLE

    # No existing value — show text prompt immediately
    context.user_data[_PENDING_EDIT_KEY] = field
    prompt = _WIZ_PROMPTS.get(field, f"Введите {label.lower()}:")
    text = f"Шаг {step + 1}/6 — {label}\n\n{prompt}"
    await _wiz_send_or_edit(update, context, text, _wizard_cancel_kb())
    return STATE_IDLE


async def _wizard_confirm_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Show final confirmation screen with summary of collected data."""
    wiz_data = context.user_data.get(_WIZ_DATA_KEY) or {}

    lines = ["Оформление ИП — проверьте данные:", ""]
    for field in _WIZ_FIELDS:
        label = _WIZ_LABELS[field]
        val = wiz_data.get(field) or "—"
        display = _format_for_display(field, val)
        lines.append(f"{label}: {display}")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить заявку", callback_data=CB_SUBMIT)],
        [InlineKeyboardButton("🔄 Начать заново", callback_data=CB_ENTRY)],
        [InlineKeyboardButton("⬅️ В Услуги", callback_data="svc_root")],
    ])
    await _wiz_send_or_edit(update, context, text, kb)
    return STATE_IDLE


async def _start_wizard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Reset wizard state and start from step 0."""
    context.user_data[_WIZ_DATA_KEY] = {}
    context.user_data.pop(_PENDING_EDIT_KEY, None)

    tg_uid = str(update.effective_user.id)
    user = await get_user(tg_uid) or {}
    return await _show_wizard_step(update, context, 0, user=user)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def svc_ip_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «🏢 Открыть ИП» в корне раздела «Услуги»."""
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    tg_uid = int(update.effective_user.id)
    internal_id = await resolve_internal_user_id(tg_user_id=tg_uid)
    if not internal_id:
        await send_or_edit(
            update,
            "Не удалось определить ваш профиль. Пройдите регистрацию.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]),
            parse_mode=None,
        )
        return STATE_IDLE

    existing = await get_active_application(int(internal_id))
    if existing and existing.get("alfabank_id"):
        return await _show_status(update, context, existing)

    return await _start_wizard(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Wizard callback handlers
# ─────────────────────────────────────────────────────────────────────────────

async def ip_wiz_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User confirms the existing profile value for current wizard step."""
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    step = context.user_data.get(_WIZ_STEP_KEY, 0)
    if step >= len(_WIZ_FIELDS):
        return await _wizard_confirm_screen(update, context)

    field = _WIZ_FIELDS[step]

    tg_uid = str(update.effective_user.id)
    user = await get_user(tg_uid) or {}
    value = _get_profile_value(user, field)

    if value:
        wiz_data = context.user_data.setdefault(_WIZ_DATA_KEY, {})
        wiz_data[field] = value

    return await _show_wizard_step(update, context, step + 1)


async def ip_wiz_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User wants to enter a new value for current wizard step."""
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    step = context.user_data.get(_WIZ_STEP_KEY, 0)
    if step >= len(_WIZ_FIELDS):
        return STATE_IDLE

    field = _WIZ_FIELDS[step]
    label = _WIZ_LABELS[field]

    context.user_data[_PENDING_EDIT_KEY] = field
    prompt = _WIZ_PROMPTS.get(field, f"Введите {label.lower()}:")
    text = f"Шаг {step + 1}/6 — {label}\n\n{prompt}"
    await _wiz_send_or_edit(update, context, text, _wizard_cancel_kb())
    return STATE_IDLE


async def ip_wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User cancels the wizard."""
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    context.user_data.pop(_WIZ_STEP_KEY, None)
    context.user_data.pop(_WIZ_DATA_KEY, None)
    context.user_data.pop(_PENDING_EDIT_KEY, None)
    context.user_data.pop(_WIZ_MSG_KEY, None)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В Услуги", callback_data="svc_root")],
    ])
    await _wiz_send_or_edit(update, context, "Оформление ИП отменено.", kb)
    return STATE_IDLE


async def ip_wiz_city_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Retry city input after FIAS not-found error."""
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    step = _WIZ_FIELDS.index("city")  # step 5
    context.user_data[_WIZ_STEP_KEY] = step

    # Remove city from wiz_data to force fresh input
    wiz_data = context.user_data.get(_WIZ_DATA_KEY) or {}
    wiz_data.pop("city", None)
    context.user_data[_WIZ_DATA_KEY] = wiz_data

    context.user_data[_PENDING_EDIT_KEY] = "city"

    text = "Шаг 6/6 — Город\n\nВведите город (например: Москва):"
    await _wiz_send_or_edit(update, context, text, _wizard_cancel_kb())
    return STATE_IDLE


# ─────────────────────────────────────────────────────────────────────────────
# Text input handler
# ─────────────────────────────────────────────────────────────────────────────

async def ip_save_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Accept text input for the pending wizard field."""
    field = context.user_data.get(_PENDING_EDIT_KEY)
    if not field:
        return STATE_IDLE

    msg = update.message
    if not msg:
        return STATE_IDLE
    raw = (msg.text or "").strip()
    tg_uid = str(update.effective_user.id)

    # ── Validate and save ───────────────────────────────────────────────
    if field == "first_name":
        if not raw:
            await msg.reply_text("Имя не может быть пустым.")
            return STATE_IDLE
        await upsert_user(tg_uid, first_name=raw)

    elif field == "last_name":
        if not raw:
            await msg.reply_text("Фамилия не может быть пустой.")
            return STATE_IDLE
        await upsert_user(tg_uid, last_name=raw)

    elif field == "email":
        if not EMAIL_RE.match(raw):
            await msg.reply_text("Некорректный e-mail. Пример: user@example.com")
            return STATE_IDLE
        await upsert_user(tg_uid, email=raw)

    elif field == "inn":
        d = _digits(raw)
        if len(d) not in (10, 12):
            await msg.reply_text(
                "Неверная длина ИНН.\n"
                "Введите 10 цифр (для ООО/ОАО) или 12 цифр (для ИП и физлица)."
            )
            return STATE_IDLE
        if not _validate_inn_checksum(d):
            await msg.reply_text(
                "ИНН не прошёл проверку контрольного числа.\n"
                "Убедитесь, что вы ввели его без ошибок."
            )
            return STATE_IDLE
        raw = d
        from db import set_user_inn  # локальный импорт
        await set_user_inn(tg_uid, d)

    elif field == "city":
        if not raw:
            await msg.reply_text("Город не может быть пустым.")
            return STATE_IDLE
        await set_user_city(tg_uid, raw)

    else:
        context.user_data.pop(_PENDING_EDIT_KEY, None)
        return STATE_IDLE

    # Store validated value in wizard data
    wiz_data = context.user_data.setdefault(_WIZ_DATA_KEY, {})
    wiz_data[field] = raw

    context.user_data.pop(_PENDING_EDIT_KEY, None)

    # Delete user's typed message (best effort)
    chat = update.effective_chat
    chat_id = int(chat.id) if chat else None
    if msg and chat_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass

    step = context.user_data.get(_WIZ_STEP_KEY, 0)
    return await _show_wizard_step(update, context, step + 1)


# ─────────────────────────────────────────────────────────────────────────────
# Submission
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_city_code(city_name: str) -> tuple[str, bool | None]:
    """Найти city_code по названию в справочнике ФИАС.

    Возвращает ``(city_code, found)`` где ``found``:
    - ``True``  — город найден, ``city_code`` — UUID ФИАС;
    - ``False`` — города нет в справочнике;
    - ``None``  — справочник временно недоступен (API-ошибка), нельзя проверить.
    """
    target = (city_name or "").strip().lower()
    if not target:
        return ("", False)
    try:
        async with AlfaBankAsyncClient(
            config=_alfabank_config(),
            dictionary_cache=_CITIES_CACHE,
        ) as client:
            cities = await client.dictionaries.cities()
        for city in cities:
            if (city.name or "").strip().lower() == target:
                return (city.code, True)
        return (city_name, False)
    except (AlfaBankAPIError, AlfaBankNetworkError, Exception) as exc:
        logger.warning("cities lookup failed: %r", exc)
        return (city_name, None)


def _build_lead_request(user: dict[str, Any], city_code: str) -> LeadCreateRequest:
    org_name = f"ИП {user.get('last_name', '').strip()} {user.get('first_name', '').strip()}".strip()
    return LeadCreateRequest(
        organization_info=OrganizationInfo(
            organization_name=org_name or "ИП",
            inn=_normalize_inn(user.get("inn")) or "",
        ),
        contact_info=[
            ContactInfo(
                full_name=_full_name(user) or None,
                phone_number=_phone_for_alfabank(user.get("phone")) or "",
                contact_email=(user.get("email") or "").strip() or None,
            )
        ],
        request_info=RequestInfo(city_code=city_code),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )


async def ip_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    tg_uid = int(update.effective_user.id)
    internal_id = await resolve_internal_user_id(tg_user_id=tg_uid)
    if not internal_id:
        await _wiz_send_or_edit(
            update, context,
            "Не удалось определить ваш профиль.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]),
        )
        return STATE_IDLE

    # Build effective user dict from wiz_data, fallback to profile for missing fields
    wiz_data = context.user_data.get(_WIZ_DATA_KEY) or {}
    profile = await get_user(str(tg_uid)) or {}

    user: dict[str, Any] = {}
    for field in _WIZ_FIELDS:
        user[field] = wiz_data.get(field) or profile.get(field)

    # Check for missing required fields
    missing = [f for f in _WIZ_FIELDS if not user.get(f)]
    if missing:
        # Restart wizard
        return await _start_wizard(update, context)

    # Resolve city code from FIAS
    city_name = (user.get("city") or "").strip()
    city_code, city_found = await _resolve_city_code(city_name)
    if city_found is False:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить город", callback_data=CB_WIZ_CITY_RETRY)],
            [InlineKeyboardButton("⬅️ В Услуги", callback_data="svc_root")],
        ])
        await _wiz_send_or_edit(
            update, context,
            f"Город {city_name} не найден в справочнике Альфа-Банка.\n\n"
            "Укажите название точнее — например: Москва, Санкт-Петербург, Екатеринбург.",
            kb,
        )
        return STATE_IDLE
    if city_found is None:
        logger.warning("city dict unavailable, submitting with raw city name: %r", city_name)

    request = _build_lead_request(user, city_code)

    try:
        async with AlfaBankAsyncClient(config=_alfabank_config()) as client:
            response = await client.leads.create(request)
    except UnacceptableCompanyError as exc:
        logger.info("Alfa-Bank rejected lead as unacceptable: %s", exc)
        await _wiz_send_or_edit(
            update, context,
            "Заявка не принята: компания/предприниматель уже есть в базе "
            "или не подходит под условия. Попробуйте позже или обратитесь к менеджеру.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="svc_root")]]),
        )
        return STATE_IDLE
    except (ServerError, ServiceUnavailableError) as exc:
        logger.warning("Alfa-Bank server error on lead create: %s", exc)
        await _wiz_send_or_edit(
            update, context,
            "В данный момент сервис регистрации недоступен, приносим извинения за неудобства.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить позже", callback_data=CB_ENTRY)]]),
        )
        return STATE_IDLE
    except AlfaBankAPIError as exc:
        logger.warning("Alfa-Bank API error on lead create: %s", exc)
        await _wiz_send_or_edit(
            update, context,
            f"Не удалось отправить заявку: {exc.title}.\n\nПопробуйте позже или скорректируйте данные.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Попробовать снова", callback_data=CB_ENTRY)]]),
        )
        return STATE_IDLE
    except AlfaBankNetworkError as exc:
        logger.warning("Alfa-Bank network error on lead create: %r", exc)
        await _wiz_send_or_edit(
            update, context,
            "В данный момент сервис регистрации недоступен, приносим извинения за неудобства.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить позже", callback_data=CB_ENTRY)]]),
        )
        return STATE_IDLE

    await create_application(
        user_id=int(internal_id),
        tg_user_id=tg_uid,
        alfabank_id=response.id,
        alfabank_raw=response.model_dump(by_alias=True, exclude_none=True),
    )

    # Clear wizard state
    context.user_data.pop(_WIZ_STEP_KEY, None)
    context.user_data.pop(_WIZ_DATA_KEY, None)
    context.user_data.pop(_WIZ_MSG_KEY, None)

    await _wiz_send_or_edit(
        update, context,
        "Заявка на открытие ИП принята.\n\n"
        f"Номер заявки: {response.id}\n\n"
        "Мы сообщим вам, когда будет принято решение. Текущий статус всегда можно "
        "проверить кнопкой «🏢 Открыть ИП» в разделе «Услуги».",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Проверить статус", callback_data=CB_ENTRY)],
            [InlineKeyboardButton("⬅️ В Услуги", callback_data="svc_root")],
        ]),
    )
    return STATE_IDLE


# ─────────────────────────────────────────────────────────────────────────────
# Status check
# ─────────────────────────────────────────────────────────────────────────────

def _map_lead_state_to_status(info: Any) -> str:
    """Маппинг LeadInfo из Alfa-Bank API → локальный статус.

    Согласно таблице статусов API (раздел 3):
    - CLOSED + LP_RKO:COMPLETED → approved (счёт открыт)
    - CLOSED + LP_RKO:DECLINED или UNDEFINED → declined
    - DECLINED (уровень заявки) → declined
    - SENT / IN_PROGRESS → pending
    """
    raw_lead = info.state.value if hasattr(info.state, "value") else info.state

    if raw_lead == LeadState.DECLINED.value:
        return "declined"

    if raw_lead == LeadState.CLOSED.value:
        for product in (getattr(info, "product_info", None) or []):
            raw_prod = product.product_code.value if hasattr(product.product_code, "value") else product.product_code
            if raw_prod == ProductCode.LP_RKO.value:
                raw_state = product.state.value if hasattr(product.state, "value") else product.state
                if raw_state == "COMPLETED":
                    return "approved"
                return "declined"
        return "declined"

    return "pending"


_STATUS_HUMAN = {
    "pending": "Заявка рассматривается\n\nРешение ещё не принято. Загляните позже.",
    "approved": "Заявка принята\n\nОткрытие ИП одобрено. С вами свяжется менеджер.",
    "declined": "Заявка отклонена\n\nК сожалению, в открытии ИП отказано.",
}


async def _show_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    application: dict[str, Any],
) -> int:
    alfabank_id = (application.get("alfabank_id") or "").strip()
    local_status = (application.get("status") or "pending").strip()
    state_for_message: str | None = None

    try:
        async with AlfaBankAsyncClient(config=_alfabank_config()) as client:
            info = await client.leads.get(alfabank_id)
        new_status = _map_lead_state_to_status(info)
        state_for_message = (
            info.state.value if hasattr(info.state, "value") else str(info.state)
        )
        if new_status != local_status:
            await update_application_status(
                application_id=int(application["id"]),
                status=new_status,
                alfabank_state=state_for_message,
                alfabank_raw=info.model_dump(by_alias=True, exclude_none=True),
            )
            local_status = new_status
        else:
            await update_application_status(
                application_id=int(application["id"]),
                status=local_status,
                alfabank_state=state_for_message,
                alfabank_raw=info.model_dump(by_alias=True, exclude_none=True),
            )
    except (ServerError, ServiceUnavailableError) as exc:
        logger.warning("Alfa-Bank server error on lead get: %s", exc)
    except AlfaBankAPIError as exc:
        logger.warning("Alfa-Bank API error on lead get: %s", exc)
    except AlfaBankNetworkError as exc:
        logger.warning("Alfa-Bank network error on lead get: %r", exc)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while fetching lead status")

    body = _STATUS_HUMAN.get(local_status, _STATUS_HUMAN["pending"])
    text = f"{body}\n\nНомер заявки: {alfabank_id}"
    if state_for_message:
        text += f"\nТекущее состояние в банке: {state_for_message}"

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🔄 Обновить статус", callback_data=CB_REFRESH_STATUS)],
        [InlineKeyboardButton("⬅️ В Услуги", callback_data="svc_root")],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(rows), parse_mode=None)
    return STATE_IDLE


async def ip_refresh_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        try:
            await q.answer("Запрашиваю статус…")
        except Exception:
            pass
    tg_uid = int(update.effective_user.id)
    internal_id = await resolve_internal_user_id(tg_user_id=tg_uid)
    if not internal_id:
        return STATE_IDLE
    existing = await get_active_application(int(internal_id))
    if not existing or not existing.get("alfabank_id"):
        return await _start_wizard(update, context)
    return await _show_status(update, context, existing)
