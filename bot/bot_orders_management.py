# bot_orders_management.py
"""
Пока управление заказами со стороны директора, впоследствии постараемся перенести сюда всё,
связанное с заказами
"""

from typing import Optional, Dict, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import os
import sys

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
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
    _get_user_tg_id,
    get_order
    ,
    _get_user_id_by_tg,
    get_director_specialist,
    ensure_order_chat,
    delete_order_chat,
    ORDER_CHAT_REQUEST_TYPE
)

import os
import time
import json
import asyncio
import logging
from datetime import datetime

from telegram.error import BadRequest

from bot_common import (
    STATE_IDLE,
    STATE_DIR_SET_SERVICE_CODE,
    send_or_edit,
    chat_webapp_button
)

logger = logging.getLogger(__name__)

import concurrent.futures

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

async def show_users_with_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    '''
    Экран с меню выбора пользователей с текущими заказами
    '''
    await update.callback_query.answer()
    
    users = await get_users_with_orders()
    rows: list[list[InlineKeyboardButton]] = []
    
    for user in users:
        rows.append([InlineKeyboardButton(user['first_name'], callback_data=f"user_with_orders:{user['id']}")])
    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")])
    
    if len(users) == 0:
        text = "Пока заказов нет"
    else:
        text = "Пользователи, заказы которых в обработке: "
    await send_or_edit(
        update,
        text,
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
        context=context
    )
    
    return STATE_IDLE

async def show_orders_of_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    '''
    Экран с меню выбора заказов пользователя
    '''
    q = update.callback_query
    await q.answer()
    
    _, user_id = q.data.split(":", 1)
    user_id = int(user_id)
    
    orders = await get_orders_of_user(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    
    for order in orders:
        rows.append([InlineKeyboardButton(f"Заказ #{order['id']} от \t{order['created_at'].strftime('%d.%m.%Y')}", callback_data=f"show_order:{order['id']}")])
    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")])
    
    inn = await get_user_inn_by_id(user_id)
    phone = await get_user_phone_by_id(user_id)
    kkt_number = await get_user_kkt_factory_number_by_id(user_id)
    await send_or_edit(
        update,
        f"ИНН: {inn}\n\nТелефон: {phone}\n\nНомер ККТ: {kkt_number}\n\nЗаказы пользователя:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
        context=context
    )
    
    return STATE_IDLE


async def order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Меню заказа (директор).
    Показываем заказанное оборудование + заказанные коды (services_for_orders) и даём кнопки для ручной вставки кодов.
    """
    q = update.callback_query
    await q.answer()

    _, order_id = q.data.split(":", 1)
    try:
        order_id = int(order_id)
    except Exception:
        return STATE_IDLE

    # оборудование
    eq_items = await get_order_items_equipment(order_id) or []
    lines = ["<b>Заказанное оборудование:</b>"]
    if not eq_items:
        lines.append("—")
    else:
        for n, item in enumerate(eq_items, 1):
            used = " б/у" if item.get("is_used") else ""
            lines.append(f"{n}) {item.get('name')}{used}, {int(item.get('qty') or 1)} шт.")

    # коды по services_for_orders
    code_items = await get_order_items_codes(order_id) or []
    lines.append("")
    lines.append("<b>Заказанные коды (ручная выдача):</b>")
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

    # гарантируем чат по заказу (чтобы директор мог написать первым)
    try:
        ord_row = await get_order(order_id) or {}
        client_internal_id = int(ord_row.get("user_id") or 0)
    except Exception:
        client_internal_id = 0

    director = await get_director_specialist() or {}
    director_id = int(director.get("id") or 0)

    chat_id: int | None = None
    # Чат по заказу хранит канонический internal users.id клиента.
    if client_internal_id and director_id:
        try:
            chat_id = await ensure_order_chat(
                order_id=order_id,
                client_user_id=client_internal_id,
                director_specialist_id=director_id,
                source="telegram",
            )
        except Exception:
            chat_id = None

    # кнопки
    rows: list[list[InlineKeyboardButton]] = []

    # чат
    if chat_id:
        rows.append([
            await chat_webapp_button(
                chat_id=int(chat_id),
                role="agent",
                actor_id=int(update.effective_user.id),
                text="💬 Чат с клиентом",
            )
        ])

    # отдельная кнопка на каждый НЕзаполненный код
    for idx, it in enumerate(code_items, 1):
        code_val = (it.get("code_value") or "").strip()
        if code_val:
            continue
        code_id = int(it.get("code_id") or 0)
        if not code_id:
            continue
        rows.append([InlineKeyboardButton(f"✏️ Вставить код #{idx}", callback_data=f"dir_srv_code:{order_id}:{code_id}")])

    rows.append([InlineKeyboardButton("✅ Закрыть заказ как выполненный", callback_data=f"close_order_start:{order_id}")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="users_with_orders")])
    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")])

    await send_or_edit(
        update,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context,
    )
    return STATE_IDLE


async def dir_set_service_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Старт ручного ввода кода директором."""
    q = update.callback_query
    await q.answer()

    try:
        _, order_id, code_id = q.data.split(":", 2)
        order_id = int(order_id)
        code_id = int(code_id)
    except Exception:
        return STATE_IDLE
    
    user_id = (await get_order(order_id))['user_id']
    context.user_data["dir_srv_code"] = {"order_id": order_id, "code_id": code_id, "user_id": user_id}

    await send_or_edit(
        update,
        f"✏️ Введите код для заказа <b>#{order_id}</b>\n\n"
        "Код можно вставить одним сообщением.\n"
        "Чтобы отменить — нажмите «Назад».",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"show_order:{order_id}")]]),
    )
    return STATE_DIR_SET_SERVICE_CODE


async def dir_set_service_code_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимаем текст кода и сохраняем в БД."""
    msg = update.message
    if not msg:
        return STATE_IDLE

    raw = (msg.text or "").strip()
    payload = (context.user_data or {}).get("dir_srv_code") or {}
    order_id = payload.get("order_id")
    code_id = payload.get("code_id")

    if not order_id or not code_id:
        await msg.reply_text("⚠️ Не удалось определить заказ/код. Откройте заказ заново.")
        return STATE_IDLE

    if not raw:
        await msg.reply_text("Пришлите код текстом (не пустой).")
        return STATE_DIR_SET_SERVICE_CODE

    ok = await set_service_order_code_value(int(code_id), raw)
    if not ok:
        await msg.reply_text("❌ Не удалось сохранить код. Проверьте, что код корректный и попробуйте ещё раз.")
        return STATE_DIR_SET_SERVICE_CODE

    # чистим контекст
    context.user_data.pop("dir_srv_code", None)

    await send_or_edit(
        update,
        "✅ Код сохранён.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К заказу", callback_data=f"show_order:{order_id}")]]),
    )
    await send_notification_about_code(payload.get("user_id"), context)
    return STATE_IDLE


async def close_order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    '''
    Дополнительно подтверждаем закрытие заказа
    '''
    q = update.callback_query
    await q.answer()
    
    _, order_id = q.data.split(":", 1)
    order_id = int(order_id)
    
    await send_or_edit(
        update,
        "Вы точно хотите закрыть заказ?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Да", callback_data=f"close_order_finish:{order_id}")],
            [InlineKeyboardButton("Нет", callback_data=f"show_order:{order_id}")]
        ]),
        parse_mode="Markdown",
        context=context
    )
    
    return STATE_IDLE

async def close_order_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    '''
    Закрытие заказа
    '''
    q = update.callback_query
    await q.answer()
    
    _, order_id = q.data.split(":", 1)
    order_id = int(order_id)
    
    await set_order_finished(order_id)

    # по договоренности: при выполнении заказа — удаляем чат по заказу
    try:
        await delete_order_chat(order_id)
    except Exception:
        pass
    try:
        text = f"Заказ {order_id} закрыт"
    except Exception:
        text = "Произошла ошибка"
    await send_or_edit(
        update,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]
        ]),
        parse_mode="Markdown",
        context=context
    )
    
    return STATE_IDLE


async def send_notification_about_code(user_id: str, context: ContextTypes.DEFAULT_TYPE):
    tg_user_id = await _get_user_tg_id(user_id)
    print(tg_user_id)
    await context.application.bot.send_message(
            chat_id=tg_user_id,
            text="Вам выдан код по заказу. Перейдите в профиль, чтобы посмотреть.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Мой профиль", callback_data="profile")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]
            ]),
            disable_web_page_preview=True,
        )

#теперь интерфейс для покупателя
#чаты о заказах
async def show_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню клиента: список его заказов (кнопка "Мои заказы" из профиля)."""
    q = update.callback_query
    if q:
        await q.answer()

    tg_uid = int(update.effective_user.id)
    internal_user_id = await _get_user_id_by_tg(tg_uid)
    if not internal_user_id:
        await send_or_edit(
            update,
            "Не удалось найти ваш профиль. Напишите /start и попробуйте снова.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]),
            parse_mode=ParseMode.HTML,
            context=context,
        )
        return STATE_IDLE

    orders = await get_orders_of_user(int(internal_user_id))
    rows: list[list[InlineKeyboardButton]] = []
    for order in orders:
        dt = order["created_at"].strftime('%d.%m.%Y') if order.get("created_at") else ""
        rows.append([
            InlineKeyboardButton(
                f"Заказ #{order['id']} от {dt}",
                callback_data=f"my_order_menu:{order['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="profile")])
    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")])

    text = "<b>Мои заказы</b>\n\nВыберите заказ, чтобы открыть чат и увидеть состав заказа."
    await send_or_edit(
        update,
        text,
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context,
    )
    return STATE_IDLE


async def my_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Экран клиента по конкретному заказу: состав + кнопка WebApp-чата."""
    q = update.callback_query
    await q.answer()
    _, order_id = q.data.split(":", 1)
    order_id = int(order_id)

    ord_row = await get_order(order_id) or {}
    client_user_id = int(ord_row.get("user_id") or 0)
    if not client_user_id:
        await send_or_edit(
            update,
            "Не удалось открыть заказ.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="my_orders")]]),
            parse_mode=ParseMode.HTML,
            context=context,
        )
        return STATE_IDLE

    director = await get_director_specialist() or {}
    director_id = int(director.get("id") or 0)
    chat_id = await ensure_order_chat(
        order_id=order_id,
        client_user_id=client_user_id,
        director_specialist_id=director_id,
        source="telegram",
    )

    # оборудование
    eq_items = await get_order_items_equipment(order_id) or []
    lines = [f"<b>Заказ #{order_id}</b>"]
    lines.append("")
    lines.append("<b>Оборудование:</b>")
    if not eq_items:
        lines.append("—")
    else:
        for n, item in enumerate(eq_items, 1):
            used = " б/у" if item.get("is_used") else ""
            lines.append(f"{n}) {item.get('name')}{used}, {int(item.get('qty') or 1)} шт.")

    # коды (ручная выдача)
    code_items = await get_order_items_codes(order_id) or []
    lines.append("")
    lines.append("<b>Коды:</b>")
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

    kb = [
        [
            await chat_webapp_button(
                chat_id=int(chat_id),
                role="client",
                actor_id=int(update.effective_user.id),
                text="💬 Открыть чат по заказу",
            )
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_orders")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ]

    await send_or_edit(
        update,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML,
        context=context,
    )
    return STATE_IDLE
