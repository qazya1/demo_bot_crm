"""
max_handlers.py — Обработчики команд и кнопок MAX-бота.

Структура повторяет логику Telegram-бота (bot_registration.py, bot_common.py),
но адаптирована под MAX API.
"""

import logging
import os
import sys
from pathlib import Path

_bot_dir = Path(__file__).resolve().parent
_project_dir = _bot_dir.parent
sys.path.insert(0, str(_project_dir))

from max_api import MaxMessage, MaxCallback, InlineKeyboard, InlineButton
from max_bot_main import MaxBot, MaxBotContext
from notice_center import clear_user_notices_max

from max_registration import (
    handle_text_by_state,
    _show_main_menu,
    _send_or_edit,
    _resolve_chat_id,
    register_registration_handlers,
)
from max_services import register_services_handlers
from max_client_chat import register_client_chat_handlers, handle_cc_text_by_state
from max_catalog import register_catalog_handlers
from max_referral import register_referral_handlers, referral_trigger
from max_alfabank_ip import register_alfabank_ip_handlers, handle_ip_text_by_state

# specialist + orders (director) modules
from max_agent_chat import register_agent_chat_handlers, handle_ag_text_by_state
from max_orders_management import register_orders_management_handlers, handle_orders_text_by_state

logger = logging.getLogger(__name__)


# ── Обработчики callback-ов ──────────────────────────────────────────


async def _send_callback_fallback(ctx: MaxBotContext, cb: MaxCallback, text: str, keyboard: InlineKeyboard) -> None:
    if cb.message and cb.message.chat_id:
        await ctx.api.send_message(cb.message.chat_id, text, keyboard=keyboard)
        return
    if cb.user:
        await ctx.api.send_message_to_user(cb.user.user_id, text, keyboard=keyboard)
        return
    logger.warning("callback fallback failed: no message.chat_id and no cb.user")


async def handle_back_to_main(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Кнопка «В меню» — показываем динамическое главное меню."""
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    old_menu_mid = str(ctx.user_data.get("_menu_msg_id") or "").strip()
    if cb.user:
        ctx.user_data.setdefault("_max_uid", str(cb.user.user_id))
        try:
            await clear_user_notices_max(
                max_api=ctx.api,
                max_user_id=int(cb.user.user_id),
            )
        except Exception:
            logger.debug("best-effort MAX back_to_main notice cleanup failed for uid=%s", cb.user.user_id, exc_info=True)
    # Если callback прилетел из notice/служебного сообщения (а не из текущего меню),
    # удаляем его явно и рендерим меню как новое сообщение.
    use_mid_for_menu = mid
    cur_mid = str(mid or "").strip()
    if cur_mid and cur_mid != old_menu_mid:
        use_mid_for_menu = None
        try:
            await ctx.api.delete_message(cur_mid)
        except Exception:
            logger.debug("best-effort delete callback source message failed: mid=%s", cur_mid, exc_info=True)
    # Сбрасываем контекст клиентского чата
    ctx.user_data.pop("cc_reply_chat_id", None)
    ctx.user_data.pop("client_chat_ctx", None)
    await _show_main_menu(ctx, chat_id, "Чем могу помочь?", message_id=use_mid_for_menu)


async def handle_text(ctx: MaxBotContext, msg: MaxMessage) -> None:
    """
    Обработчик произвольного текста.
    Сначала проверяем, ожидает ли state-машина ввода (регистрация/редактирование).
    Затем — состояния каталога (ввод email для оплаты), специалиста, заказов, клиентского чата.
    Если нет — предлагаем вернуться в меню.
    """
    if not msg.sender:
        return

    # 1) Регистрация/профиль
    handled = await handle_text_by_state(ctx, msg)
    if handled:
        return

    # 2) Checkout: ввод email/адреса в состоянии co_email / co_address
    # Логика находится в max_catalog.py, но текст приходит сюда.
    try:
        from max_catalog import checkout_email_input, checkout_address_input

        internal_id = int(ctx.user_data.get("_db_id") or 0)
        if internal_id:
            handled = await checkout_email_input(ctx, msg, msg.chat_id, internal_id)
            if handled:
                return
            handled = await checkout_address_input(ctx, msg, msg.chat_id, internal_id)
            if handled:
                return
    except Exception:
        # Не ломаем обработку текста, даже если каталог отключён или импорт упал
        pass

    # 3) Открытие ИП: ввод полей профиля
    handled = await handle_ip_text_by_state(ctx, msg)
    if handled:
        return

    # 4) Специалист (/agent: ввод телефона и т.п.)
    handled = await handle_ag_text_by_state(ctx, msg)
    if handled:
        return

    # 5) Управление заказами (директор: ручной ввод кода)
    handled = await handle_orders_text_by_state(ctx, msg)
    if handled:
        return

    # 6) Клиентский чат (создание обращения/ответ)
    handled = await handle_cc_text_by_state(ctx, msg)
    if handled:
        return

    # 7) Реферальный 4-значный код (секретный триггер)
    handled = await referral_trigger(ctx, msg)
    if handled:
        return

    # Fallback
    kb = InlineKeyboard(rows=[
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(
        ctx, msg.chat_id,
        "Я пока не понимаю свободный текст. Воспользуйтесь меню:",
        keyboard=kb,
    )


async def handle_stub(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Заглушка для разделов, которые ещё не реализованы."""
    section_names = {
        "cart": "Корзина",
        "buy": "Магазин",
    }
    payload = cb.payload or ""
    name = section_names.get(payload, payload)

    text = f"🚧 Раздел «{name}» пока в разработке.\n\nСкоро будет доступен!"
    kb = InlineKeyboard(rows=[
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])

    if cb.message and cb.message.message_id:
        await ctx.api.edit_message(cb.message.message_id, text, keyboard=kb)
    else:
        await _send_callback_fallback(ctx, cb, text, kb)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _validate_callback_wiring(bot: MaxBot) -> None:
    """Sanity-check: печать callback pattern-ов и fail-fast на конфликтах."""
    patterns = [p for p, _ in bot._callback_handlers]

    # Полный список callback-pattern-ов в порядке регистрации
    logger.info("MAX callback patterns (%d): %s", len(patterns), ", ".join(patterns))

    # Fail-fast: дубли критичных pattern-ов (одинаковый pattern зарегистрирован >1 раза)
    critical = {
        "register", "profile", "back_to_main", "services", "cart", "buy", "checkout", "my_chats"
    }
    seen = {}
    dup_critical = []
    for idx, p in enumerate(patterns):
        if p in critical:
            if p in seen:
                dup_critical.append((p, seen[p], idx))
            else:
                seen[p] = idx

    if dup_critical:
        details = "; ".join([f"{p} at #{i1} and #{i2}" for p, i1, i2 in dup_critical])
        raise RuntimeError(f"Critical callback wiring conflict detected: {details}")


# ── Регистрация всех обработчиков ────────────────────────────────────


def register_handlers(bot: MaxBot) -> None:
    """
    Единая compose-точка wiring для MAX.

    Управляется env-флагами:
    - MAX_ENABLE_CATALOG (default: true)
    - MAX_ENABLE_REFERRAL (default: true)
    - MAX_ENABLE_NOTIFICATIONS (default: true, informational only)
    """
    enable_catalog = _env_flag("MAX_ENABLE_CATALOG", True)
    enable_referral = _env_flag("MAX_ENABLE_REFERRAL", True)
    enable_notifications = _env_flag("MAX_ENABLE_NOTIFICATIONS", True)

    # 1) Базовые модули
    register_registration_handlers(bot)
    register_services_handlers(bot)
    register_alfabank_ip_handlers(bot)
    register_client_chat_handlers(bot)
    register_agent_chat_handlers(bot)
    register_orders_management_handlers(bot)

    # 2) Опциональные доменные блоки
    if enable_catalog:
        register_catalog_handlers(bot)
        logger.info("MAX wiring: catalog enabled")
    else:
        # Если каталог выключен — оставляем явные заглушки на buy/cart
        for pattern in ("cart", "buy"):
            bot.register_callback(pattern, handle_stub)
        logger.info("MAX wiring: catalog disabled (cart/buy stubs enabled)")

    if enable_referral:
        register_referral_handlers(bot)
        logger.info("MAX wiring: referral enabled")
    else:
        logger.info("MAX wiring: referral disabled")

    # В текущем коде max_notifications — это job-функции, не callback handlers.
    # Здесь только явно фиксируем статус, чтобы wiring был прозрачен.
    if enable_notifications:
        logger.info("MAX wiring: notifications enabled (jobs should be scheduled in main runtime)")
    else:
        logger.info("MAX wiring: notifications disabled")

    # 3) Навигация + fallback
    bot.register_callback("back_to_main", handle_back_to_main)
    bot.register_message(handle_text)

    # 4) Sanity-check wiring
    _validate_callback_wiring(bot)

    logger.info(
        "MAX-бот: handlers wired (catalog=%s, referral=%s, notifications=%s)",
        enable_catalog,
        enable_referral,
        enable_notifications,
    )
