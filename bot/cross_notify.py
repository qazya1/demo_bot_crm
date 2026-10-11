"""
cross_notify.py — Кросс-платформенные уведомления специалистам.

При создании обращения клиентом (из TG или MAX) уведомления должны
уходить специалисту на ОБЕ платформы (TG + MAX), если у него есть
соответствующие ID.
"""

import os
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
MAX_API_BASE_URL = "https://platform-api.max.ru"

RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 1
RETRY_DELAY = 1.0


def _normalize_max_buttons(buttons: list[list[dict]] | None) -> list[list[dict]] | None:
    if not buttons:
        return None

    normalized: list[list[dict]] = []
    for row in buttons:
        if not isinstance(row, list):
            continue
        new_row: list[dict] = []
        for btn in row:
            if not isinstance(btn, dict):
                continue

            if btn.get("type"):
                new_row.append(btn)
                continue

            text = btn.get("text") or ""
            payload = btn.get("payload") or btn.get("callback_data") or ""
            url = btn.get("url") or ""

            if url:
                new_row.append({"type": "link", "text": text, "url": url})
            elif payload:
                new_row.append({"type": "callback", "text": text, "payload": payload})

        if new_row:
            normalized.append(new_row)

    return normalized or None



async def notify_specialist_tg(
    tg_user_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> int:
    """
    Отправить уведомление специалисту в Telegram.
    Возвращает message_id (0 при ошибке).
    """
    if not BOT_TOKEN:
        logger.debug("BOT_TOKEN not set, skipping TG notify")
        return 0
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload: dict = {
        "chat_id": int(tg_user_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    data = r.json() or {}
                    return int(((data.get("result") or {}).get("message_id")) or 0)
                elif r.status_code in RETRIABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    logger.warning("TG sendMessage retriable error: %s (attempt %d)", r.status_code, attempt + 1)
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logger.error("TG sendMessage error: %s %s", r.status_code, r.text[:200])
        except Exception as e:
            if attempt < MAX_RETRIES:
                logger.warning("TG notify error (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY)
                continue
            logger.error("TG notify error: %s", e)
    return 0


async def notify_specialist_max(
    max_user_id: int,
    text: str,
    keyboard_buttons: list[list[dict]] | None = None,
) -> str:
    """
    Отправить уведомление специалисту в MAX.
    Возвращает message_id (пустая строка при ошибке).
    """
    if not MAX_BOT_TOKEN:
        logger.debug("MAX_BOT_TOKEN not set, skipping MAX notify")
        return ""
    params = {"user_id": str(max_user_id)}
    payload: dict = {"text": text}
    keyboard_buttons = _normalize_max_buttons(keyboard_buttons)
    if keyboard_buttons:
        payload["attachments"] = [{
            "type": "inline_keyboard",
            "payload": {"buttons": keyboard_buttons},
        }]
    headers = {"Authorization": MAX_BOT_TOKEN}
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{MAX_API_BASE_URL}/messages",
                    params=params, json=payload, headers=headers,
                )
                if r.status_code == 200:
                    data = r.json() or {}
                    # MAX API response format: {"message": {"body": {"mid": "..."}}}
                    mid = (data.get("message") or {}).get("body", {}).get("mid", "")
                    return str(mid or data.get("message_id") or data.get("id") or "")
                elif r.status_code in RETRIABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    logger.warning("MAX sendMessage retriable error: %s (attempt %d)", r.status_code, attempt + 1)
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logger.error("MAX sendMessage error: %s %s", r.status_code, r.text[:200])
        except Exception as e:
            if attempt < MAX_RETRIES:
                logger.warning("MAX notify error (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY)
                continue
            logger.error("MAX notify error: %s", e)
    return ""


async def notify_specialist_all_platforms(
    specialist: dict,
    text_html: str,
    *,
    tg_reply_markup: dict | None = None,
    max_keyboard_buttons: list[list[dict]] | None = None,
) -> tuple[int, str]:
    """
    Отправить уведомление специалисту на ОБЕ платформы (TG + MAX).
    Возвращает (tg_message_id, max_message_id).
    """
    tg_uid = int(specialist.get("tg_user_id") or 0)
    max_uid = int(specialist.get("max_user_id") or 0)

    tg_mid = 0
    max_mid = ""

    if tg_uid:
        tg_mid = await notify_specialist_tg(tg_uid, text_html, reply_markup=tg_reply_markup)
    if max_uid:
        max_mid = await notify_specialist_max(max_uid, text_html, keyboard_buttons=max_keyboard_buttons)

    if tg_uid and max_uid:
        tg_ok = bool(tg_mid)
        max_ok = bool(max_mid)
        if tg_ok != max_ok:
            logger.warning(
                "Partial delivery to specialist %s: TG=%s MAX=%s",
                specialist.get("id", "?"),
                "OK" if tg_ok else "FAIL",
                "OK" if max_ok else "FAIL",
            )

    return tg_mid, max_mid
