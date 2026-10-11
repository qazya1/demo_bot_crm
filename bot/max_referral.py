"""
max_referral.py — Реферальная программа для MAX-бота.

Перенос логики из bot_registration.py (TG):
- Показ реферального QR-кода (только верифицированным)
- Секретные 4-значные реф-коды (пользователь пишет «1111» → бот фиксирует)
- Регистрация обработчиков
"""

import hashlib
import logging
import os
import re

from max_api import MaxCallback, MaxMessage, InlineKeyboard, InlineButton
from max_bot_main import MaxBotContext
from webapp_settings import (
    MAX_BOT_NAME,
    MAX_REF_BOT_NAME,
    MAX_REF_FALLBACK_BOT_NAME,
    TG_REF_BOT_USERNAME,
)

logger = logging.getLogger(__name__)

REFERRAL_CODE_RE = re.compile(r"^\d{4}$")
MAX_QR_NAMESPACE = "max_referral"


def _build_max_referral_url(ref_code: str) -> str:
    """Собирает корректный deep-link для MAX-бота с реферальным payload."""
    bot_name = (MAX_REF_BOT_NAME or MAX_BOT_NAME or MAX_REF_FALLBACK_BOT_NAME or "").strip().lstrip("@")
    if not bot_name:
        logger.warning("MAX referral bot name is empty (MAX_REF_BOT_NAME/MAX_BOT_NAME/MAX_REF_FALLBACK_BOT_NAME)")
        return ""
    return f"https://max.ru/{bot_name}?start=ref_{ref_code}"


def _build_tg_referral_url(ref_code: str) -> str:
    """Ссылка на Telegram deep-link с реферальным payload."""
    bot_name = (TG_REF_BOT_USERNAME or "").strip().lstrip("@")
    if not bot_name:
        logger.warning("TG referral bot username is empty (TG_REF_BOT_USERNAME)")
        return ""
    return f"https://t.me/{bot_name}?start=ref_{ref_code}"


# ══════════════════════════════════════════════════════════════════════
# Утилиты (импорт из max_registration — переиспользуем)
# ══════════════════════════════════════════════════════════════════════

from max_registration import (
    _resolve_chat_id,
    _resolve_db_id,
    _send_or_edit,
    _set_state,
    ST_IDLE,
)

from db import (
    is_user_verified_by_internal_id,
    get_specialist_by_max,
    get_or_create_specialist_ref_code,
    get_user_ref_code,
    create_user_ref_code,
    get_referral_code,
    apply_referral_code,
)


# ══════════════════════════════════════════════════════════════════════
# Показ реферального QR-кода
# ══════════════════════════════════════════════════════════════════════

async def show_referral_qr(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """
    Кнопка «Реферальный QR» — генерируем QR-код и отправляем фото.

    - Доступно только верифицированным пользователям.
    - QR содержит ссылку с реф-кодом пользователя.
    - Файл генерируется один раз и кэшируется.
    """
    if not cb.user:
        return

    max_uid = str(cb.user.user_id or ctx.user_data.get("_max_uid") or "").strip()
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    if not chat_id:
        chat_id = str(ctx.user_data.get("_chat_id") or "").strip()
    mid = cb.message.message_id if cb.message else None
    data = ((cb.payload or "") if cb else "").strip()
    selected_platform = ""
    if data.startswith("my_referral_qr:"):
        selected_platform = data.split(":", 1)[1].strip().lower()

    if not chat_id and not max_uid:
        logger.error("show_referral_qr: no recipient resolved (chat_id/max_uid empty)")
        return
    db_id = await _resolve_db_id(ctx, max_uid)

    if selected_platform not in ("telegram", "max"):
        kb = InlineKeyboard(rows=[
            [InlineButton("Telegram", callback_data="my_referral_qr:telegram")],
            [InlineButton("MAX", callback_data="my_referral_qr:max")],
            [InlineButton("⬅ В профиль", callback_data="profile")],
        ])
        await _send_or_edit(
            ctx, chat_id,
            "Выберите платформу для QR-кода:",
            keyboard=kb, message_id=mid,
        )
        return

    # Проверка верификации
    verified = await is_user_verified_by_internal_id(db_id)
    if not verified:
        kb = InlineKeyboard(rows=[
            [InlineButton("🔐 Закончить верификацию", callback_data="register")],
            [InlineButton("👤 В профиль", callback_data="profile")],
        ])
        await _send_or_edit(
            ctx, chat_id,
            "⛔ Реферальный QR доступен только после верификации профиля.\n\n"
            "Подтвердите телефон и укажите ИНН.",
            keyboard=kb, message_id=mid,
        )
        return

    # Получаем / создаём реф-код:
    # для партнёра profession=5 используем единый specialist ref_code
    specialist = await get_specialist_by_max(
        max_user_id=int(max_uid) if str(max_uid).isdigit() else None,
        max_username=(cb.user.username or "").strip() or None,
    )
    if specialist and int(specialist.get("profession") or 0) == 5:
        specialist_id = int(specialist.get("id") or 0)
        owner_name = (specialist.get("name") or "").strip() or f"Специалист #{specialist_id}"
        ref_code = await get_or_create_specialist_ref_code(specialist_id, owner_name=owner_name)
    else:
        ref_code = await get_user_ref_code(internal_id=db_id)
        if not ref_code:
            ref_code = await create_user_ref_code(internal_id=db_id)

    if not ref_code:
        kb = InlineKeyboard(rows=[
            [InlineButton("👤 В профиль", callback_data="profile")],
        ])
        await _send_or_edit(
            ctx, chat_id,
            "Не удалось создать реферальный код. Попробуйте позже.",
            keyboard=kb, message_id=mid,
        )
        return

    # Формируем ссылку — MAX deep link
    # Пользователь переходит по ссылке → /start ref_XXXX
    referral_url = _build_max_referral_url(ref_code) if selected_platform == "max" else _build_tg_referral_url(ref_code)
    if not referral_url:
        kb = InlineKeyboard(rows=[[InlineButton("👤 В профиль", callback_data="profile")]])
        await _send_or_edit(
            ctx, chat_id,
            "⚠️ Не настроена ссылка для выбранной платформы.",
            keyboard=kb, message_id=mid,
        )
        return

    # Генерируем QR-файл
    base_dir = os.path.dirname(os.path.abspath(__file__))
    qr_dir = os.path.join(base_dir, "images", "qrs")
    os.makedirs(qr_dir, exist_ok=True)
    qr_namespace = MAX_QR_NAMESPACE if selected_platform == "max" else "telegram_referral"

    qr_path = None
    try:
        from qr_utils import generate_qr_file_hashed
        qr_path = generate_qr_file_hashed(
            referral_url,
            out_dir=qr_dir,
            namespace=qr_namespace,
        )
    except Exception:
        qr_path = None

    # Fallback: md5-имя
    if not qr_path:
        qr_hash_base = f"{qr_namespace}|{referral_url}"
        qr_path = os.path.join(qr_dir, f"{hashlib.md5(qr_hash_base.encode('utf-8')).hexdigest()}.png")
        if not os.path.exists(qr_path):
            try:
                from qr_utils import generate_qr_file_hashed
                generate_qr_file_hashed(
                    referral_url,
                    out_dir=qr_dir,
                    namespace=qr_namespace,
                )
            except Exception:
                pass

    # Отправляем фото
    if qr_path and os.path.exists(qr_path):
        try:
            with open(qr_path, "rb") as f:
                photo_data = f.read()

            kb = InlineKeyboard(rows=[
                [InlineButton("⬅ В профиль", callback_data="profile")],
            ])
            await ctx.api.send_photo(
                chat_id,
                photo_data,
                user_id=max_uid,
                caption=(
                    "Вы можете поделиться этим QR-кодом, позвав к нам новых клиентов "
                    "и получив дополнительные бонусы!\n\n"
                    f"Ссылка для приглашения:\n{referral_url}"
                ),
                filename="referral_qr.png",
                keyboard=kb,
            )

            # Удаляем старое сообщение (профиль) после отправки QR
            if mid:
                try:
                    await ctx.api.delete_message(mid)
                except Exception:
                    pass
            return
        except Exception as e:
            logger.error("send_photo QR error: %s", e)

    # Fallback — если не удалось отправить QR фото
    kb = InlineKeyboard(rows=[
        [InlineButton("⬅ В профиль", callback_data="profile")],
    ])
    await _send_or_edit(
        ctx, chat_id,
        f"Ваш реферальный код: {ref_code}\n\n"
        f"Ссылка для приглашения:\n{referral_url}\n\n"
        "Поделитесь ссылкой с друзьями и получите бонусы!",
        keyboard=kb, message_id=mid,
    )


# ══════════════════════════════════════════════════════════════════════
# Секретные 4-значные реф-коды
# ══════════════════════════════════════════════════════════════════════

async def referral_trigger(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """
    Проверяет, является ли текст секретным 4-значным реф-кодом.

    Если пользователь пишет ровно 4 цифры (1111, 2222 и т.п.)
    и такой код существует в БД → фиксируем реферала.

    Возвращает True если сообщение обработано (чтобы handle_text остановился).
    """
    if not msg.sender:
        return False

    raw = (msg.text or "").strip()
    if not REFERRAL_CODE_RE.fullmatch(raw):
        return False

    # Проверяем, есть ли такой код в БД
    rc = await get_referral_code(raw)
    if not rc:
        return False  # молча игнорируем — не палим механику

    max_uid = msg.sender.user_id
    db_id = await _resolve_db_id(ctx, str(max_uid))

    res = await apply_referral_code(raw_code=raw, raw_text=raw, internal_id=db_id, source_platform="max")

    if res.get("ok") and res.get("status") == "applied":
        await ctx.api.send_message(msg.chat_id, "Мы зафиксировали ✅ Спасибо!")
    elif res.get("ok") and res.get("status") == "already_set":
        await ctx.api.send_message(msg.chat_id, "Код уже был зафиксирован ранее ✅")
    else:
        await ctx.api.send_message(msg.chat_id, "Не удалось зафиксировать код. Попробуйте позже.")

    return True


# ══════════════════════════════════════════════════════════════════════
# Регистрация обработчиков
# ══════════════════════════════════════════════════════════════════════

def register_referral_handlers(bot) -> None:
    """Подключить обработчики реферальной программы к MaxBot."""
    bot.register_callback("my_referral_qr", show_referral_qr)
    bot.register_callback("my_referral_qr:telegram", show_referral_qr)
    bot.register_callback("my_referral_qr:max", show_referral_qr)
    logger.info("MAX-бот: обработчики реферальной программы подключены")
