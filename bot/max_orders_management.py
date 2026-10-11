"""
max_orders_management.py — Управление заказами для директоров (MAX-бот).

Перенос логики из bot_orders_management.py:
- Список пользователей с заказами
- Заказы пользователя (инфо: ИНН, телефон, ККТ)
- Детали заказа (оборудование + коды)
- Ручной ввод кода для сервисного заказа
- Закрытие заказа как выполненного
- Клиентский интерфейс: список заказов + чат по заказу

Адаптация для MAX:
- Plain text вместо HTML parse_mode
- InlineKeyboard/InlineButton из max_api
- State-машина через user_data["_state"]
- WebApp-кнопка для чата по заказу
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from max_api import MaxMessage, MaxCallback, InlineKeyboard, InlineButton
from max_bot_main import MaxBot, MaxBotContext
from max_registration import _send_or_edit, _set_state, ST_IDLE, _resolve_chat_id

_bot_dir = Path(__file__).resolve().parent
_project_dir = _bot_dir.parent
sys.path.insert(0, str(_project_dir))

from db import (
    get_users_with_orders,
    get_orders_of_user,
    set_order_finished,
    get_order_items_equipment,
    get_order_items_codes,
    set_service_order_code_value,
    get_user_kkt_factory_number_by_id,
    get_user_inn_by_id,
    get_user_phone_by_id,
    get_order,
    get_director_specialist,
    ensure_order_chat,
    delete_order_chat,
    get_user_by_internal_id,
    create_chat_access_token,
    upsert_max_user,
)

logger = logging.getLogger(__name__)

from webapp_settings import WEBAPP_URL

# ── Состояние ─────────────────────────────────────────────────────────
ST_DIR_SET_SERVICE_CODE = "dir_set_service_code"


# ── Helpers ───────────────────────────────────────────────────────────

async def _chat_webapp_button(
    chat_id: int,
    role: str,
    actor_id: int | str,
    text: str = "💬 Ответить",
) -> InlineButton:
    token = await create_chat_access_token(
        chat_id=chat_id,
        role=role,
        actor_id=str(actor_id),
    )
    url = f"{WEBAPP_URL}/chat?t={token}"
    return InlineButton(text, web_app=url)


async def _get_max_user_id_from_internal(internal_id: int) -> Optional[int]:
    """Получить max_user_id пользователя по внутреннему users.id."""
    user = await get_user_by_internal_id(internal_id)
    if user and user.get("max_user_id"):
        return int(user["max_user_id"])
    return None


# ══════════════════════════════════════════════════════════════════════
# Директорская часть: список пользователей с заказами
# ══════════════════════════════════════════════════════════════════════

async def handle_users_with_orders(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """users_with_orders — список пользователей с текущими заказами."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    users = await get_users_with_orders()
    rows = []
    for user in users:
        rows.append([InlineButton(user['first_name'], callback_data=f"user_with_orders:{user['id']}")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    text = "Пока заказов нет" if not users else "Пользователи, заказы которых в обработке:"
    await _send_or_edit(ctx, chat_id, text, keyboard=InlineKeyboard(rows=rows), message_id=mid)
    _set_state(ctx, ST_IDLE)


# ══════════════════════════════════════════════════════════════════════
# Заказы пользователя
# ══════════════════════════════════════════════════════════════════════

async def handle_orders_of_user(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """user_with_orders:<user_id> — список заказов пользователя."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    user_id = int((cb.payload or "").split(":", 1)[1])
    orders = await get_orders_of_user(user_id)

    rows = []
    for order in orders:
        dt = order['created_at'].strftime('%d.%m.%Y') if order.get('created_at') else ""
        rows.append([InlineButton(f"Заказ #{order['id']} от {dt}", callback_data=f"show_order:{order['id']}")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    inn = await get_user_inn_by_id(user_id)
    phone = await get_user_phone_by_id(user_id)
    kkt_number = await get_user_kkt_factory_number_by_id(user_id)

    text = f"ИНН: {inn}\nТелефон: {phone}\nНомер ККТ: {kkt_number}\n\nЗаказы пользователя:"
    await _send_or_edit(ctx, chat_id, text, keyboard=InlineKeyboard(rows=rows), message_id=mid)
    _set_state(ctx, ST_IDLE)


# ══════════════════════════════════════════════════════════════════════
# Детали заказа (директор)
# ══════════════════════════════════════════════════════════════════════

async def handle_order_menu(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """show_order:<order_id> — детали заказа для директора."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    order_id = int((cb.payload or "").split(":", 1)[1])

    # оборудование
    eq_items = await get_order_items_equipment(order_id) or []
    lines = ["Заказанное оборудование:"]
    if not eq_items:
        lines.append("—")
    else:
        for n, item in enumerate(eq_items, 1):
            used = " б/у" if item.get("is_used") else ""
            lines.append(f"{n}) {item.get('name')}{used}, {int(item.get('qty') or 1)} шт.")

    # коды
    code_items = await get_order_items_codes(order_id) or []
    lines.append("")
    lines.append("Заказанные коды (ручная выдача):")
    if not code_items:
        lines.append("—")
    else:
        for n, it in enumerate(code_items, 1):
            vendor = (it.get("vendor_name") or "service").strip()
            op = it.get("operator_id")
            tp = it.get("type_id")
            mn = it.get("months_number")
            code_val = (it.get("code_value") or "").strip()
            status = "✅ выдан" if code_val else "⏳ ожидает ввода"
            tail = f" ({mn} мес.)" if mn else ""
            lines.append(f"{n}) {vendor} {op}:{tp}{tail} — {status}")

    # гарантируем чат по заказу
    try:
        ord_row = await get_order(order_id) or {}
        client_internal_id = int(ord_row.get("user_id") or 0)
    except Exception:
        client_internal_id = 0

    director = await get_director_specialist() or {}
    director_id = int(director.get("id") or 0)

    order_chat_id = None
    if client_internal_id and director_id:
        try:
            order_chat_id = await ensure_order_chat(
                order_id=order_id,
                client_user_id=client_internal_id,
                director_specialist_id=director_id,
                source="max",
            )
        except Exception:
            order_chat_id = None

    # кнопки
    rows = []
    if order_chat_id and ctx.user_id:
        try:
            webapp_btn = await _chat_webapp_button(
                chat_id=int(order_chat_id),
                role="agent",
                actor_id=ctx.user_id,
                text="💬 Чат с клиентом",
            )
            rows.append([webapp_btn])
        except Exception:
            pass

    # кнопки на ввод незаполненных кодов
    for idx, it in enumerate(code_items, 1):
        code_val = (it.get("code_value") or "").strip()
        if code_val:
            continue
        code_id = int(it.get("code_id") or 0)
        if not code_id:
            continue
        rows.append([InlineButton(f"✏️ Вставить код #{idx}", callback_data=f"dir_srv_code:{order_id}:{code_id}")])

    rows.append([InlineButton("✅ Закрыть заказ как выполненный", callback_data=f"close_order_start:{order_id}")])
    rows.append([InlineButton("🔙 Назад", callback_data="users_with_orders")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    await _send_or_edit(ctx, chat_id, "\n".join(lines), keyboard=InlineKeyboard(rows=rows), message_id=mid)
    _set_state(ctx, ST_IDLE)


# ══════════════════════════════════════════════════════════════════════
# Ручной ввод кода
# ══════════════════════════════════════════════════════════════════════

async def handle_dir_set_service_code_start(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """dir_srv_code:<order_id>:<code_id> — начало ввода кода."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    parts = (cb.payload or "").split(":", 2)
    order_id = int(parts[1])
    code_id = int(parts[2])

    ord_row = await get_order(order_id) or {}
    user_id = ord_row.get("user_id")

    ctx.user_data["dir_srv_code"] = {"order_id": order_id, "code_id": code_id, "user_id": user_id}

    kb = InlineKeyboard(rows=[[InlineButton("⬅️ Назад", callback_data=f"show_order:{order_id}")]])
    await _send_or_edit(ctx, chat_id,
        f"✏️ Введите код для заказа #{order_id}\n\n"
        "Код можно вставить одним сообщением.\n"
        "Чтобы отменить — нажмите «Назад».",
        keyboard=kb, message_id=mid)
    _set_state(ctx, ST_DIR_SET_SERVICE_CODE)


async def _on_dir_service_code_text(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Обработка текстового ввода кода. Возвращает True если обработано."""
    state = ctx.user_data.get("_state")
    if state != ST_DIR_SET_SERVICE_CODE:
        return False

    raw = (msg.text or "").strip()
    payload = ctx.user_data.get("dir_srv_code") or {}
    order_id = payload.get("order_id")
    code_id = payload.get("code_id")

    if not order_id or not code_id:
        await ctx.api.send_message(msg.chat_id, "⚠️ Не удалось определить заказ/код. Откройте заказ заново.")
        _set_state(ctx, ST_IDLE)
        return True

    if not raw:
        await ctx.api.send_message(msg.chat_id, "Пришлите код текстом (не пустой).")
        return True

    ok = await set_service_order_code_value(int(code_id), raw)
    if not ok:
        await ctx.api.send_message(msg.chat_id, "❌ Не удалось сохранить код. Попробуйте ещё раз.")
        return True

    ctx.user_data.pop("dir_srv_code", None)

    kb = InlineKeyboard(rows=[[InlineButton("⬅️ К заказу", callback_data=f"show_order:{order_id}")]])
    await _send_or_edit(ctx, msg.chat_id, "✅ Код сохранён.", keyboard=kb)
    _set_state(ctx, ST_IDLE)

    # Уведомляем клиента через MAX
    user_id = payload.get("user_id")
    if user_id:
        max_uid = await _get_max_user_id_from_internal(int(user_id))
        if max_uid:
            try:
                kb_client = InlineKeyboard(rows=[
                    [InlineButton("Мой профиль", callback_data="profile")],
                    [InlineButton("🔙 В меню", callback_data="back_to_main")],
                ])
                await ctx.api.send_message_to_user(
                    max_uid,
                    "Вам выдан код по заказу. Перейдите в профиль, чтобы посмотреть.",
                    keyboard=kb_client,
                )
            except Exception as e:
                logger.warning("notify client about code: %s", e)

    return True


# ══════════════════════════════════════════════════════════════════════
# Закрытие заказа
# ══════════════════════════════════════════════════════════════════════

async def handle_close_order_confirm(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """close_order_start:<order_id> — подтверждение закрытия."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    order_id = int((cb.payload or "").split(":", 1)[1])

    kb = InlineKeyboard(rows=[
        [InlineButton("Да", callback_data=f"close_order_finish:{order_id}")],
        [InlineButton("Нет", callback_data=f"show_order:{order_id}")],
    ])
    await _send_or_edit(ctx, chat_id, "Вы точно хотите закрыть заказ?", keyboard=kb, message_id=mid)


async def handle_close_order_yes(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """close_order_finish:<order_id> — закрытие заказа."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    order_id = int((cb.payload or "").split(":", 1)[1])

    await set_order_finished(order_id)

    # удаляем чат по заказу
    try:
        await delete_order_chat(order_id)
    except Exception:
        pass

    kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
    await _send_or_edit(ctx, chat_id, f"Заказ {order_id} закрыт", keyboard=kb, message_id=mid)
    _set_state(ctx, ST_IDLE)


# ══════════════════════════════════════════════════════════════════════
# Клиентская часть: мои заказы
# ══════════════════════════════════════════════════════════════════════

async def handle_my_orders(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """my_orders — список заказов клиента."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    max_uid = ctx.user_id
    if not max_uid:
        return

    internal_id = await upsert_max_user(max_uid)
    if not internal_id:
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id,
            "Не удалось найти ваш профиль. Напишите /start и попробуйте снова.",
            keyboard=kb, message_id=mid)
        return

    orders = await get_orders_of_user(int(internal_id))
    rows = []
    for order in orders:
        dt = order["created_at"].strftime('%d.%m.%Y') if order.get("created_at") else ""
        rows.append([InlineButton(f"Заказ #{order['id']} от {dt}", callback_data=f"my_order_menu:{order['id']}")])
    rows.append([InlineButton("🔙 Назад", callback_data="profile")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    text = "Мои заказы\n\nВыберите заказ, чтобы открыть чат и увидеть состав заказа."
    await _send_or_edit(ctx, chat_id, text, keyboard=InlineKeyboard(rows=rows), message_id=mid)


async def handle_my_order_menu(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """my_order_menu:<order_id> — детали заказа для клиента."""
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    order_id = int((cb.payload or "").split(":", 1)[1])

    ord_row = await get_order(order_id) or {}
    client_user_id = int(ord_row.get("user_id") or 0)
    if not client_user_id:
        kb = InlineKeyboard(rows=[[InlineButton("🔙 Назад", callback_data="my_orders")]])
        await _send_or_edit(ctx, chat_id, "Не удалось открыть заказ.", keyboard=kb, message_id=mid)
        return

    director = await get_director_specialist() or {}
    director_id = int(director.get("id") or 0)
    order_chat_id = await ensure_order_chat(
        order_id=order_id,
        client_user_id=client_user_id,
        director_specialist_id=director_id,
        source="max",
    )

    # оборудование
    eq_items = await get_order_items_equipment(order_id) or []
    lines = [f"Заказ #{order_id}", ""]
    lines.append("Оборудование:")
    if not eq_items:
        lines.append("—")
    else:
        for n, item in enumerate(eq_items, 1):
            used = " б/у" if item.get("is_used") else ""
            lines.append(f"{n}) {item.get('name')}{used}, {int(item.get('qty') or 1)} шт.")

    # коды
    code_items = await get_order_items_codes(order_id) or []
    lines.append("")
    lines.append("Коды:")
    if not code_items:
        lines.append("—")
    else:
        for n, it in enumerate(code_items, 1):
            vendor = (it.get("vendor_name") or "service").strip()
            op = it.get("operator_id")
            tp = it.get("type_id")
            mn = it.get("months_number")
            code_val = (it.get("code_value") or "").strip()
            status = "✅ выдан" if code_val else "⏳ в обработке"
            tail = f" ({mn} мес.)" if mn else ""
            lines.append(f"{n}) {vendor} {op}:{tp}{tail} — {status}")

    rows = []
    if order_chat_id and max_uid:
        try:
            webapp_btn = await _chat_webapp_button(
                chat_id=int(order_chat_id),
                role="client",
                actor_id=max_uid,
                text="💬 Открыть чат по заказу",
            )
            rows.append([webapp_btn])
        except Exception:
            pass
    rows.append([InlineButton("🔙 Назад", callback_data="my_orders")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    await _send_or_edit(ctx, chat_id, "\n".join(lines), keyboard=InlineKeyboard(rows=rows), message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# State router
# ══════════════════════════════════════════════════════════════════════

async def handle_orders_text_by_state(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Маршрутизация текста по state. Возвращает True если обработано."""
    return await _on_dir_service_code_text(ctx, msg)


# ══════════════════════════════════════════════════════════════════════
# Регистрация обработчиков
# ══════════════════════════════════════════════════════════════════════

def register_orders_management_handlers(bot: MaxBot) -> None:
    """Подключить все обработчики управления заказами к MAX-боту."""
    # Директор
    bot.register_callback("users_with_orders", handle_users_with_orders)
    bot.register_callback("user_with_orders:*", handle_orders_of_user)
    bot.register_callback("show_order:*", handle_order_menu)
    bot.register_callback("dir_srv_code:*", handle_dir_set_service_code_start)
    bot.register_callback("close_order_start:*", handle_close_order_confirm)
    bot.register_callback("close_order_finish:*", handle_close_order_yes)

    # Клиент
    bot.register_callback("my_orders", handle_my_orders)
    bot.register_callback("my_order_menu:*", handle_my_order_menu)

    logger.info("MAX orders management handlers registered")
