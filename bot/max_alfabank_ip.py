"""max_alfabank_ip.py — Заявка на открытие ИП через Альфа-Банк для MAX-бота.

Точка входа — кнопка «🏢 Открыть ИП» (callback_data="svc_ip").
Логика зеркалит bot_alfabank_ip.py, адаптирована под MAX API.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from max_api import MaxCallback, MaxMessage, InlineKeyboard, InlineButton
from max_bot_main import MaxBotContext
from max_registration import (
    _send_or_edit,
    _send_text,
    _set_state,
    _get_state,
    ST_IDLE,
    _resolve_chat_id,
    _ensure_db_id_from_sender,
    EMAIL_RE,
)

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

from db import (  # noqa: E402
    create_application,
    get_active_application,
    update_application_status,
)
from db.users import resolve_internal_user_id  # noqa: E402

logger = logging.getLogger(__name__)

# ── Состояния текстового ввода ─────────────────────────────────────────
# Kept for backward compatibility — wizard uses a single state
ST_IP_EDIT_FIRST = "ip_edit_first"
ST_IP_EDIT_LAST = "ip_edit_last"
ST_IP_EDIT_EMAIL = "ip_edit_email"
ST_IP_EDIT_INN = "ip_edit_inn"
ST_IP_EDIT_CITY = "ip_edit_city"

# Single wizard text-input state
ST_IP_WIZARD = "ip_wizard"

_IP_STATES = {ST_IP_EDIT_FIRST, ST_IP_EDIT_LAST, ST_IP_EDIT_EMAIL, ST_IP_EDIT_INN, ST_IP_EDIT_CITY, ST_IP_WIZARD}

# Кэш справочника городов (TTL 24 ч)
_CITIES_CACHE = DictionaryCache(ttl=24 * 60 * 60)

import re as _re
_DIGITS_RE = _re.compile(r"\D+")

# ── user_data keys ─────────────────────────────────────────────────────
_PENDING_EDIT_KEY = "_ip_pending_edit"
_WIZ_STEP_KEY = "_ip_wiz_step"
_WIZ_DATA_KEY = "_ip_wiz_data"
_WIZ_MSG_KEY = "_ip_wiz_msg_id"

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


def _alfabank_config() -> ClientConfig:
    api_key = os.getenv("ALFABANK_API_KEY") or DEMO_API_KEY
    base_url = os.getenv("ALFABANK_BASE_URL") or "https://partner.alfabank.ru/public-api/v2"
    return ClientConfig(api_key=api_key, base_url=base_url)


def _digits(s: str | None) -> str:
    return _DIGITS_RE.sub("", s or "")


def _phone_for_alfabank(phone: str | None) -> str | None:
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
    raw = (user.get(field) or "").strip()
    return raw if raw else None


def _format_for_display(field: str, value: str) -> str:
    if field == "phone":
        return _format_phone_human(value)
    return value


def _wizard_cancel_kb() -> InlineKeyboard:
    return InlineKeyboard(rows=[[InlineButton("🚫 Отмена", callback_data="ip_wiz_cancel")]])


# ── Загрузка профиля пользователя ─────────────────────────────────────

async def _load_user_profile(ctx: MaxBotContext, db_id: int) -> dict[str, Any]:
    """Собрать поля профиля из user_data (кэш) или БД."""
    needed = ("first_name", "last_name", "phone", "email", "inn", "city")
    if all(k in ctx.user_data for k in needed):
        return {k: ctx.user_data.get(k) for k in needed}

    from db import get_user_by_internal_id
    try:
        row = await get_user_by_internal_id(db_id) or {}
    except Exception:
        row = {}

    for k in needed:
        ctx.user_data.setdefault(k, row.get(k))

    return {k: ctx.user_data.get(k) for k in needed}


# ── send_or_edit для wizard ────────────────────────────────────────────

async def _wiz_send_or_edit(
    ctx: MaxBotContext,
    chat_id: str,
    text: str,
    keyboard: InlineKeyboard,
) -> None:
    """Try to edit the stored wizard message; fall back to sending a new one."""
    wiz_mid = ctx.user_data.get(_WIZ_MSG_KEY)
    result_mid = await _send_or_edit(ctx, chat_id, text, keyboard=keyboard, message_id=wiz_mid)
    if result_mid:
        ctx.user_data[_WIZ_MSG_KEY] = result_mid


# ── Wizard step rendering ──────────────────────────────────────────────

async def _show_wizard_step(
    ctx: MaxBotContext,
    chat_id: str,
    step: int,
    user: dict[str, Any] | None = None,
) -> None:
    """Show wizard step for the given step index (0-5)."""
    if step >= 6:
        await _wizard_confirm_screen(ctx, chat_id)
        return

    ctx.user_data[_WIZ_STEP_KEY] = step
    ctx.user_data.pop(_PENDING_EDIT_KEY, None)

    field = _WIZ_FIELDS[step]
    label = _WIZ_LABELS[field]

    if user is None:
        db_id = int(ctx.user_data.get("_db_id") or 0)
        user = await _load_user_profile(ctx, db_id) if db_id else {}

    existing = _get_profile_value(user, field)

    if field == "phone":
        if existing:
            text = (
                f"Шаг {step + 1}/6 — {label}\n\n"
                f"В профиле: {_format_for_display(field, existing)}\n\n"
                "Использовать этот номер?"
            )
            kb = InlineKeyboard(rows=[
                [InlineButton("✅ Использовать", callback_data="ip_wiz_confirm")],
                [InlineButton("🚫 Отмена", callback_data="ip_wiz_cancel")],
            ])
        else:
            text = (
                f"Шаг {step + 1}/6 — {label}\n\n"
                "Телефон не найден в профиле.\n"
                "Чтобы добавить номер, откройте Мой профиль и привяжите телефон "
                "через кнопку «Поделиться контактом»."
            )
            kb = InlineKeyboard(rows=[
                [InlineButton("👤 Мой профиль", callback_data="profile")],
                [InlineButton("🚫 Отмена", callback_data="ip_wiz_cancel")],
            ])
        await _wiz_send_or_edit(ctx, chat_id, text, kb)
        _set_state(ctx, ST_IDLE)
        return

    # Non-phone field
    if existing:
        text = (
            f"Шаг {step + 1}/6 — {label}\n\n"
            f"В профиле: {_format_for_display(field, existing)}\n\n"
            "Использовать это значение?"
        )
        kb = InlineKeyboard(rows=[
            [InlineButton("✅ Использовать", callback_data="ip_wiz_confirm"),
             InlineButton("✏️ Ввести другое", callback_data="ip_wiz_change")],
            [InlineButton("🚫 Отмена", callback_data="ip_wiz_cancel")],
        ])
        await _wiz_send_or_edit(ctx, chat_id, text, kb)
        _set_state(ctx, ST_IDLE)
        return

    # No existing value — show text prompt immediately
    ctx.user_data[_PENDING_EDIT_KEY] = field
    prompt = _WIZ_PROMPTS.get(field, f"Введите {label.lower()}:")
    text = f"Шаг {step + 1}/6 — {label}\n\n{prompt}"
    await _wiz_send_or_edit(ctx, chat_id, text, _wizard_cancel_kb())
    _set_state(ctx, ST_IP_WIZARD)


async def _wizard_confirm_screen(ctx: MaxBotContext, chat_id: str) -> None:
    """Show final confirmation screen with summary of collected data."""
    wiz_data = ctx.user_data.get(_WIZ_DATA_KEY) or {}

    lines = ["Оформление ИП — проверьте данные:", ""]
    for field in _WIZ_FIELDS:
        label = _WIZ_LABELS[field]
        val = wiz_data.get(field) or "—"
        display = _format_for_display(field, val)
        lines.append(f"{label}: {display}")

    text = "\n".join(lines)
    kb = InlineKeyboard(rows=[
        [InlineButton("✅ Отправить заявку", callback_data="ip_submit")],
        [InlineButton("🔄 Начать заново", callback_data="svc_ip")],
        [InlineButton("⬅️ В Услуги", callback_data="svc_root")],
    ])
    await _wiz_send_or_edit(ctx, chat_id, text, kb)
    _set_state(ctx, ST_IDLE)


async def _start_wizard(ctx: MaxBotContext, chat_id: str, db_id: int) -> None:
    """Reset wizard state and start from step 0."""
    ctx.user_data[_WIZ_DATA_KEY] = {}
    ctx.user_data.pop(_PENDING_EDIT_KEY, None)
    user = await _load_user_profile(ctx, db_id)
    await _show_wizard_step(ctx, chat_id, 0, user=user)


# ── Точка входа ────────────────────────────────────────────────────────

async def handle_svc_ip(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «🏢 Открыть ИП»."""
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    try:
        db_id = await _ensure_db_id_from_sender(ctx, max_uid)
    except Exception as exc:
        logger.exception("handle_svc_ip: resolve db_id failed: %s", exc)
        await _send_or_edit(
            ctx, chat_id,
            "Не удалось определить ваш профиль. Пройдите регистрацию.",
            keyboard=InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]]),
            message_id=mid,
        )
        return

    existing = await get_active_application(int(db_id))
    if existing and existing.get("alfabank_id"):
        await _show_status(ctx, chat_id, existing, message_id=mid)
        return

    # Update wizard message key with current mid so first step edits it
    if mid:
        ctx.user_data[_WIZ_MSG_KEY] = mid
    await _start_wizard(ctx, chat_id, db_id)


# ── Wizard callback handlers ───────────────────────────────────────────

async def handle_ip_wiz_confirm(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """User confirms the existing profile value for current wizard step."""
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    if mid:
        ctx.user_data[_WIZ_MSG_KEY] = mid

    step = ctx.user_data.get(_WIZ_STEP_KEY, 0)
    if step >= len(_WIZ_FIELDS):
        await _wizard_confirm_screen(ctx, chat_id)
        return

    field = _WIZ_FIELDS[step]

    try:
        db_id = await _ensure_db_id_from_sender(ctx, max_uid)
    except Exception:
        return

    user = await _load_user_profile(ctx, db_id)
    value = _get_profile_value(user, field)

    if value:
        wiz_data = ctx.user_data.setdefault(_WIZ_DATA_KEY, {})
        wiz_data[field] = value

    await _show_wizard_step(ctx, chat_id, step + 1)


async def handle_ip_wiz_change(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """User wants to enter a new value for current wizard step."""
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    if mid:
        ctx.user_data[_WIZ_MSG_KEY] = mid

    step = ctx.user_data.get(_WIZ_STEP_KEY, 0)
    if step >= len(_WIZ_FIELDS):
        return

    field = _WIZ_FIELDS[step]
    label = _WIZ_LABELS[field]

    ctx.user_data[_PENDING_EDIT_KEY] = field
    prompt = _WIZ_PROMPTS.get(field, f"Введите {label.lower()}:")
    text = f"Шаг {step + 1}/6 — {label}\n\n{prompt}"
    await _wiz_send_or_edit(ctx, chat_id, text, _wizard_cancel_kb())
    _set_state(ctx, ST_IP_WIZARD)


async def handle_ip_wiz_cancel(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """User cancels the wizard."""
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    if mid:
        ctx.user_data[_WIZ_MSG_KEY] = mid

    ctx.user_data.pop(_WIZ_STEP_KEY, None)
    ctx.user_data.pop(_WIZ_DATA_KEY, None)
    ctx.user_data.pop(_PENDING_EDIT_KEY, None)
    ctx.user_data.pop(_WIZ_MSG_KEY, None)

    kb = InlineKeyboard(rows=[
        [InlineButton("⬅️ В Услуги", callback_data="svc_root")],
    ])
    await _send_or_edit(ctx, chat_id, "Оформление ИП отменено.", keyboard=kb, message_id=mid)
    _set_state(ctx, ST_IDLE)


async def handle_ip_wiz_city_retry(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Retry city input after FIAS not-found error."""
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    if mid:
        ctx.user_data[_WIZ_MSG_KEY] = mid

    step = _WIZ_FIELDS.index("city")  # step 5
    ctx.user_data[_WIZ_STEP_KEY] = step

    wiz_data = ctx.user_data.get(_WIZ_DATA_KEY) or {}
    wiz_data.pop("city", None)
    ctx.user_data[_WIZ_DATA_KEY] = wiz_data

    ctx.user_data[_PENDING_EDIT_KEY] = "city"

    text = "Шаг 6/6 — Город\n\nВведите город (например: Москва):"
    await _wiz_send_or_edit(ctx, chat_id, text, _wizard_cancel_kb())
    _set_state(ctx, ST_IP_WIZARD)


# ── Обработка текстового ввода ─────────────────────────────────────────

async def handle_ip_text_by_state(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Обрабатывает текстовый ввод в состоянии мастера ИП."""
    state = _get_state(ctx)
    if state not in _IP_STATES:
        return False

    if not msg.sender:
        return True

    max_uid = str(msg.sender.user_id)
    chat_id = msg.chat_id
    raw = (msg.text or "").strip()

    field = ctx.user_data.get(_PENDING_EDIT_KEY)
    if not field:
        # State set but no pending field — clear state and ignore
        _set_state(ctx, ST_IDLE)
        return True

    try:
        db_id = await _ensure_db_id_from_sender(ctx, max_uid)
    except Exception as exc:
        logger.exception("handle_ip_text_by_state: resolve db_id failed: %s", exc)
        await _send_text(ctx, chat_id, "Временная ошибка. Попробуйте ещё раз.")
        return True

    from db import update_user_by_internal_id

    if field == "first_name":
        if not raw:
            await _send_text(ctx, chat_id, "Имя не может быть пустым.")
            return True
        await update_user_by_internal_id(db_id, first_name=raw)
        ctx.user_data["first_name"] = raw

    elif field == "last_name":
        if not raw:
            await _send_text(ctx, chat_id, "Фамилия не может быть пустой.")
            return True
        await update_user_by_internal_id(db_id, last_name=raw)
        ctx.user_data["last_name"] = raw

    elif field == "email":
        if not EMAIL_RE.match(raw):
            await _send_text(ctx, chat_id, "Некорректный e-mail. Пример: user@example.com")
            return True
        await update_user_by_internal_id(db_id, email=raw)
        ctx.user_data["email"] = raw

    elif field == "inn":
        d = _digits(raw)
        if len(d) not in (10, 12):
            await _send_text(
                ctx, chat_id,
                "Неверная длина ИНН.\n"
                "Введите 10 цифр (для ООО/ОАО) или 12 цифр (для ИП и физлица).",
            )
            return True
        if not _validate_inn_checksum(d):
            await _send_text(
                ctx, chat_id,
                "ИНН не прошёл проверку контрольного числа.\n"
                "Убедитесь, что вы ввели его без ошибок.",
            )
            return True
        await update_user_by_internal_id(db_id, inn=d)
        ctx.user_data["inn"] = d
        raw = d

    elif field == "city":
        if not raw:
            await _send_text(ctx, chat_id, "Город не может быть пустым.")
            return True
        await update_user_by_internal_id(db_id, city=raw)
        ctx.user_data["city"] = raw

    else:
        ctx.user_data.pop(_PENDING_EDIT_KEY, None)
        _set_state(ctx, ST_IDLE)
        return True

    # Store validated value in wizard data
    wiz_data = ctx.user_data.setdefault(_WIZ_DATA_KEY, {})
    wiz_data[field] = raw
    ctx.user_data.pop(_PENDING_EDIT_KEY, None)

    step = ctx.user_data.get(_WIZ_STEP_KEY, 0)
    await _show_wizard_step(ctx, chat_id, step + 1)
    return True


# ── Отправка заявки ────────────────────────────────────────────────────

async def _resolve_city_code(city_name: str) -> tuple[str, bool | None]:
    """Найти ФИАС-UUID города.

    Возвращает (code, found):
    - True  — найден, code — UUID
    - False — не найден в справочнике
    - None  — справочник временно недоступен
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


async def handle_ip_submit(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    if mid:
        ctx.user_data[_WIZ_MSG_KEY] = mid

    try:
        db_id = await _ensure_db_id_from_sender(ctx, max_uid)
    except Exception:
        await _send_or_edit(
            ctx, chat_id,
            "Не удалось определить ваш профиль.",
            keyboard=InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]]),
            message_id=mid,
        )
        return

    # Build effective user dict from wiz_data, fallback to profile for missing fields
    wiz_data = ctx.user_data.get(_WIZ_DATA_KEY) or {}
    profile = await _load_user_profile(ctx, db_id)

    user: dict[str, Any] = {}
    for field in _WIZ_FIELDS:
        user[field] = wiz_data.get(field) or profile.get(field)

    # Check for missing required fields
    missing = [f for f in _WIZ_FIELDS if not user.get(f)]
    if missing:
        await _start_wizard(ctx, chat_id, db_id)
        return

    city_name = (user.get("city") or "").strip()
    city_code, city_found = await _resolve_city_code(city_name)
    if city_found is False:
        kb = InlineKeyboard(rows=[
            [InlineButton("✏️ Изменить город", callback_data="ip_wiz_city_retry")],
            [InlineButton("⬅️ В Услуги", callback_data="svc_root")],
        ])
        await _wiz_send_or_edit(
            ctx, chat_id,
            f"Город {city_name} не найден в справочнике Альфа-Банка.\n\n"
            "Укажите название точнее — например: Москва, Санкт-Петербург, Екатеринбург.",
            kb,
        )
        return
    if city_found is None:
        logger.warning("city dict unavailable, submitting with raw city name: %r", city_name)

    request = _build_lead_request(user, city_code)

    try:
        async with AlfaBankAsyncClient(config=_alfabank_config()) as client:
            response = await client.leads.create(request)
    except UnacceptableCompanyError as exc:
        logger.info("Alfa-Bank rejected lead as unacceptable: %s", exc)
        await _wiz_send_or_edit(
            ctx, chat_id,
            "Заявка не принята: компания/предприниматель уже есть в базе "
            "или не подходит под условия. Попробуйте позже или обратитесь к менеджеру.",
            InlineKeyboard(rows=[[InlineButton("⬅️ Назад", callback_data="svc_root")]]),
        )
        return
    except (ServerError, ServiceUnavailableError) as exc:
        logger.warning("Alfa-Bank server error on lead create: %s", exc)
        await _wiz_send_or_edit(
            ctx, chat_id,
            "В данный момент сервис регистрации недоступен, приносим извинения за неудобства.",
            InlineKeyboard(rows=[[InlineButton("🔄 Повторить позже", callback_data="svc_ip")]]),
        )
        return
    except AlfaBankAPIError as exc:
        logger.warning("Alfa-Bank API error on lead create: %s", exc)
        await _wiz_send_or_edit(
            ctx, chat_id,
            f"Не удалось отправить заявку: {exc.title}.\n\nПопробуйте позже или скорректируйте данные.",
            InlineKeyboard(rows=[[InlineButton("🔄 Попробовать снова", callback_data="svc_ip")]]),
        )
        return
    except AlfaBankNetworkError as exc:
        logger.warning("Alfa-Bank network error on lead create: %r", exc)
        await _wiz_send_or_edit(
            ctx, chat_id,
            "В данный момент сервис регистрации недоступен, приносим извинения за неудобства.",
            InlineKeyboard(rows=[[InlineButton("🔄 Повторить позже", callback_data="svc_ip")]]),
        )
        return

    await create_application(
        user_id=int(db_id),
        tg_user_id=None,
        alfabank_id=response.id,
        alfabank_raw=response.model_dump(by_alias=True, exclude_none=True),
    )

    # Clear wizard state
    ctx.user_data.pop(_WIZ_STEP_KEY, None)
    ctx.user_data.pop(_WIZ_DATA_KEY, None)
    ctx.user_data.pop(_WIZ_MSG_KEY, None)

    await _wiz_send_or_edit(
        ctx, chat_id,
        "Заявка на открытие ИП принята.\n\n"
        f"Номер заявки: {response.id}\n\n"
        "Мы сообщим вам, когда будет принято решение. Текущий статус всегда "
        "можно проверить кнопкой «🏢 Открыть ИП» в разделе «Услуги».",
        InlineKeyboard(rows=[
            [InlineButton("🔄 Проверить статус", callback_data="svc_ip")],
            [InlineButton("⬅️ В Услуги", callback_data="svc_root")],
        ]),
    )


# ── Статус заявки ──────────────────────────────────────────────────────

def _map_lead_state_to_status(info: Any) -> str:
    """Маппинг LeadInfo → локальный статус.

    CLOSED + LP_RKO:COMPLETED → approved
    CLOSED иначе / DECLINED  → declined
    SENT / IN_PROGRESS       → pending
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
    ctx: MaxBotContext,
    chat_id: str,
    application: dict[str, Any],
    *,
    message_id: str | None = None,
) -> None:
    alfabank_id = (application.get("alfabank_id") or "").strip()
    local_status = (application.get("status") or "pending").strip()
    state_for_message: str | None = None

    try:
        async with AlfaBankAsyncClient(config=_alfabank_config()) as client:
            info = await client.leads.get(alfabank_id)
        new_status = _map_lead_state_to_status(info)
        state_for_message = info.state.value if hasattr(info.state, "value") else str(info.state)
        await update_application_status(
            application_id=int(application["id"]),
            status=new_status,
            alfabank_state=state_for_message,
            alfabank_raw=info.model_dump(by_alias=True, exclude_none=True),
        )
        local_status = new_status
    except (ServerError, ServiceUnavailableError) as exc:
        logger.warning("Alfa-Bank server error on lead get: %s", exc)
    except AlfaBankAPIError as exc:
        logger.warning("Alfa-Bank API error on lead get: %s", exc)
    except AlfaBankNetworkError as exc:
        logger.warning("Alfa-Bank network error on lead get: %r", exc)
    except Exception:
        logger.exception("Unexpected error while fetching lead status")

    body = _STATUS_HUMAN.get(local_status, _STATUS_HUMAN["pending"])
    text = f"{body}\n\nНомер заявки: {alfabank_id}"
    if state_for_message:
        text += f"\nТекущее состояние в банке: {state_for_message}"

    await _send_or_edit(
        ctx, chat_id, text,
        keyboard=InlineKeyboard(rows=[
            [InlineButton("🔄 Обновить статус", callback_data="ip_refresh_status")],
            [InlineButton("⬅️ В Услуги", callback_data="svc_root")],
        ]),
        message_id=message_id,
    )


async def handle_ip_refresh_status(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    max_uid = str(cb.user.user_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    try:
        db_id = await _ensure_db_id_from_sender(ctx, max_uid)
    except Exception:
        return

    existing = await get_active_application(int(db_id))
    if not existing or not existing.get("alfabank_id"):
        if mid:
            ctx.user_data[_WIZ_MSG_KEY] = mid
        await _start_wizard(ctx, chat_id, db_id)
        return

    await _show_status(ctx, chat_id, existing, message_id=mid)


# ── Регистрация обработчиков ───────────────────────────────────────────

def register_alfabank_ip_handlers(bot) -> None:
    bot.register_callback("svc_ip", handle_svc_ip)
    bot.register_callback("ip_wiz_confirm", handle_ip_wiz_confirm)
    bot.register_callback("ip_wiz_change", handle_ip_wiz_change)
    bot.register_callback("ip_wiz_cancel", handle_ip_wiz_cancel)
    bot.register_callback("ip_wiz_city_retry", handle_ip_wiz_city_retry)
    bot.register_callback("ip_submit", handle_ip_submit)
    bot.register_callback("ip_refresh_status", handle_ip_refresh_status)
    logger.info("MAX-бот: обработчики ИП (Альфа-Банк) подключены")
