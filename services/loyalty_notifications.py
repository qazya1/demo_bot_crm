"""Уведомления клиенту о начислении баллов в TG и MAX."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from bot.cross_notify import notify_specialist_tg, notify_specialist_max
from db.connection import get_conn, _cur

log = logging.getLogger(__name__)

# TTL для уведомлений о баллах (5 секунд)
LOYALTY_NOTICE_TTL_SEC = 5

_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
_MAX_API_BASE_URL = "https://platform-api.max.ru"


async def notify_client_points_accrual(
    internal_user_id: int,
    *,
    amount: int,
    event_type: str,
    description: str | None = None,
) -> None:
    """Best-effort уведомление клиенту о новом начислении баллов (автоудаление через TTL)."""
    if amount <= 0:
        return

    tg_user_id = 0
    max_user_id = 0

    try:
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    "SELECT tg_user_id, max_user_id FROM users WHERE id=%s LIMIT 1",
                    (int(internal_user_id),),
                )
                row = await c.fetchone()
                if row:
                    tg_user_id = int(row.get("tg_user_id") or 0)
                    max_user_id = int(row.get("max_user_id") or 0)
    except Exception:
        log.warning("Failed to resolve user channels for loyalty notify: user=%s", internal_user_id, exc_info=True)
        return

    if not tg_user_id and not max_user_id:
        return

    event_name = _event_display_name(event_type)
    desc = (description or "").strip()

    text = f"🎉 Начислено +{amount} балл.\nОснование: {event_name}"
    if desc:
        text += f"\n{desc}"

    tg_reply_markup = {
        "inline_keyboard": [[{"text": "🔙 В меню", "callback_data": "back_to_main"}]]
    }
    max_keyboard_buttons = [[{
        "type": "callback",
        "text": "🔙 В меню",
        "payload": "back_to_main",
    }]]

    try:
        if tg_user_id:
            mid = await notify_specialist_tg(tg_user_id, text, reply_markup=tg_reply_markup)
            log.info("Loyalty TG notify: user=%s tg_mid=%s amount=%s", internal_user_id, mid, amount)
            # Ставим на автоудаление через TTL
            if mid:
                task = asyncio.create_task(_delete_tg_msg_after_ttl(tg_user_id, mid, LOYALTY_NOTICE_TTL_SEC))
                # Сохраняем ссылку на задачу чтобы GC не удалил
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)
        if max_user_id:
            mid = await notify_specialist_max(max_user_id, text, keyboard_buttons=max_keyboard_buttons)
            log.info("Loyalty MAX notify: user=%s max_mid=%s amount=%s", internal_user_id, mid, amount)
            if mid:
                task = asyncio.create_task(_delete_max_msg_after_ttl(max_user_id, mid, LOYALTY_NOTICE_TTL_SEC))
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)
    except Exception:
        log.warning("Failed to send loyalty notify: user=%s", internal_user_id, exc_info=True)


# Хранилище фоновых задач чтобы GC не удалил их до завершения
_bg_tasks: set[asyncio.Task] = set()


async def _delete_tg_msg_after_ttl(chat_id: int, message_id: int, ttl: int) -> None:
    """Удалить TG сообщение через TTL секунд (через raw HTTP API)."""
    if not _BOT_TOKEN:
        return
    try:
        await asyncio.sleep(ttl)
        url = f"https://api.telegram.org/bot{_BOT_TOKEN}/deleteMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


async def _delete_max_msg_after_ttl(max_user_id: int, message_id: str, ttl: int) -> None:
    """Удалить MAX сообщение через TTL секунд (через raw HTTP API)."""
    if not _MAX_BOT_TOKEN:
        return
    try:
        await asyncio.sleep(ttl)
        url = f"{_MAX_API_BASE_URL}/messages"
        headers = {"Authorization": _MAX_BOT_TOKEN}
        async with httpx.AsyncClient(timeout=10) as client:
            await client.request("DELETE", url, params={"message_id": message_id}, headers=headers)
    except Exception:
        pass


def _event_display_name(event_type: str) -> str:
    names: dict[str, Any] = {
        "purchase": "Покупка",
        "first_purchase": "Первая покупка",
        "profile_field": "Заполнение профиля",
        "referral_activation": "Приглашение друга",
        "referral_verification": "Верификация приглашённого",
        "referral_verification_l2": "Верификация 2-го уровня",
        "qr_collection": "Реферальная QR-коллекция",
    }
    return str(names.get(event_type, event_type.replace("_", " ").title()))
