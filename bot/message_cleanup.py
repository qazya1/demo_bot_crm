from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest


TG_LAST_MENU_KEY = "_menu_msg_id"
TG_TEMP_MIDS_KEY = "_temp_message_ids"
MAX_LAST_MENU_KEY = "_last_menu_mid"
MAX_TEMP_MIDS_KEY = "_max_temp_message_ids"
MAX_NON_DELETABLE_KEY = "_max_non_deletable_mids"
logger = logging.getLogger(__name__)


async def safe_delete_message_tg(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int | None) -> bool:
    if not message_id:
        return False
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if any(x in msg for x in (
            "message can't be deleted",
            "message to delete not found",
            "message_id_invalid",
        )):
            logger.debug("safe_delete_message_tg: stale message %s in %s: %s", message_id, chat_id, e)
        else:
            logger.warning("safe_delete_message_tg unexpected: chat_id=%s message_id=%s: %s", chat_id, message_id, e)
        return False
    except Exception:
        logger.exception("safe_delete_message_tg failed: chat_id=%s message_id=%s", chat_id, message_id)
        return False


async def safe_edit_message_tg(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int | None,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
) -> bool:
    if not message_id:
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=int(message_id),
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return True
        if any(x in msg for x in (
            "message to edit not found",
            "message_id_invalid",
        )):
            logger.debug("safe_edit_message_tg: stale message %s in %s: %s", message_id, chat_id, e)
        else:
            logger.warning("safe_edit_message_tg bad request: chat_id=%s message_id=%s err=%s", chat_id, message_id, e)
        return False
    except Exception:
        logger.exception("safe_edit_message_tg failed: chat_id=%s message_id=%s", chat_id, message_id)
        return False


async def cleanup_temporary_messages_tg(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    keep_message_id: int | None = None,
) -> None:
    mids = context.user_data.get(TG_TEMP_MIDS_KEY) or []
    if not mids:
        return
    context.user_data[TG_TEMP_MIDS_KEY] = []
    for mid in mids:
        if keep_message_id and int(mid) == int(keep_message_id):
            continue
        await safe_delete_message_tg(context, chat_id, int(mid))


def _track_temp_tg(context: ContextTypes.DEFAULT_TYPE, message_id: int | None) -> None:
    if not message_id:
        return
    arr = context.user_data.setdefault(TG_TEMP_MIDS_KEY, [])
    if int(message_id) not in arr:
        arr.append(int(message_id))
    if len(arr) > 40:
        context.user_data[TG_TEMP_MIDS_KEY] = arr[-40:]


async def send_temporary_message_tg(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: Optional[str] = None,
) -> int | None:
    chat = update.effective_chat
    if not chat:
        return None
    msg = await chat.send_message(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    _track_temp_tg(context, int(msg.message_id))
    return int(msg.message_id)


async def send_or_replace_menu_tg(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup,
    force_send_new: bool = False,
) -> int | None:
    chat = update.effective_chat
    if not chat:
        return None
    chat_id = int(chat.id)
    q = update.callback_query
    current_mid = int(q.message.message_id) if (q and q.message) else None
    prev_mid = context.user_data.get(TG_LAST_MENU_KEY)

    used_mid: int | None = None
    if q and not force_send_new and current_mid:
        ok = await safe_edit_message_tg(
            context,
            chat_id,
            current_mid,
            text=text,
            reply_markup=reply_markup,
        )
        if ok:
            used_mid = int(current_mid)
        else:
            context.user_data.pop(TG_LAST_MENU_KEY, None)

    if used_mid is None and prev_mid and not force_send_new:
        ok = await safe_edit_message_tg(
            context,
            chat_id,
            int(prev_mid),
            text=text,
            reply_markup=reply_markup,
        )
        if ok:
            used_mid = int(prev_mid)
        else:
            context.user_data.pop(TG_LAST_MENU_KEY, None)

    if used_mid is None:
        sent = await chat.send_message(text=text, reply_markup=reply_markup)
        used_mid = int(sent.message_id)

    context.user_data[TG_LAST_MENU_KEY] = int(used_mid)
    await cleanup_temporary_messages_tg(context, chat_id, keep_message_id=used_mid)

    for mid in {prev_mid, current_mid}:
        if mid and int(mid) != int(used_mid):
            asyncio.create_task(safe_delete_message_tg(context, chat_id, int(mid)))

    return int(used_mid)


def _is_max_delete_forbidden(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    if int(result.get("http_status") or 0) != 403:
        return False
    txt = str(result.get("message") or result.get("error") or result).lower()
    return ("access.denied" in txt) or ("insufficient permissions to delete message" in txt)


def _max_non_deletable_set(ctx: Any) -> set[str]:
    s = ctx.user_data.get(MAX_NON_DELETABLE_KEY)
    if not isinstance(s, set):
        s = set()
        ctx.user_data[MAX_NON_DELETABLE_KEY] = s
    return s


async def max_safe_delete_message(api: Any, message_id: str | None, *, ctx: Any | None = None) -> bool:
    if not message_id:
        return False
    mid = str(message_id)
    if ctx is not None and mid in _max_non_deletable_set(ctx):
        return False
    try:
        result = await api.delete_message(mid)
        if bool((result or {}).get("ok")):
            return True
        if _is_max_delete_forbidden(result):
            if ctx is not None:
                _max_non_deletable_set(ctx).add(mid)
            logger.debug("max_safe_delete_message: non-deletable mid=%s", mid)
            return False
        logger.warning("max_safe_delete_message failed: message_id=%s result=%s", mid, result)
        return False
    except Exception:
        logger.exception("max_safe_delete_message failed: message_id=%s", mid)
        return False


def max_track_temp_message(ctx: Any, message_id: str | None) -> None:
    if not message_id:
        return
    arr = ctx.user_data.setdefault(MAX_TEMP_MIDS_KEY, [])
    if message_id not in arr:
        arr.append(message_id)
    if len(arr) > 60:
        ctx.user_data[MAX_TEMP_MIDS_KEY] = arr[-60:]


async def max_cleanup_temporary_messages(ctx: Any, *, keep_message_id: str | None = None) -> None:
    mids = ctx.user_data.get(MAX_TEMP_MIDS_KEY) or []
    if not mids:
        return
    ctx.user_data[MAX_TEMP_MIDS_KEY] = []
    for mid in mids:
        if keep_message_id and str(mid) == str(keep_message_id):
            continue
        await max_safe_delete_message(ctx.api, str(mid), ctx=ctx)


def extract_max_mid(send_result: dict | None) -> str:
    if not isinstance(send_result, dict):
        return ""
    msg_obj = send_result.get("message")
    if not isinstance(msg_obj, dict):
        return ""
    return str(msg_obj.get("body", {}).get("mid") or "").strip()


# ══════════════════════════════════════════════════════════════════════
# Delayed delete helpers (для ui_flow.py)
# ══════════════════════════════════════════════════════════════════════

async def schedule_delete_message_tg(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    ttl_sec: int,
) -> None:
    """
    Удалить сообщение Telegram через ttl_sec секунд (best effort, idempotent).
    """
    await asyncio.sleep(ttl_sec)
    await safe_delete_message_tg(context, chat_id, message_id)


async def schedule_delete_message_max(
    api: Any,
    chat_id: str,
    message_id: str,
    ttl_sec: int,
) -> None:
    """
    Удалить сообщение MAX через ttl_sec секунд (best effort, idempotent).
    """
    await asyncio.sleep(ttl_sec)
    await max_safe_delete_message(api, message_id)


async def safe_send_message_tg(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
) -> int | None:
    """
    Безопасно отправить сообщение в Telegram. Возвращает message_id или None.
    """
    try:
        chat = context.bot.get_chat(chat_id)
        msg = await chat.send_message(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        return int(msg.message_id)
    except Exception:
        logger.exception("safe_send_message_tg failed: chat_id=%s", chat_id)
        return None


async def safe_send_message_max(
    api: Any,
    chat_id: str,
    text: str,
    *,
    keyboard: Any = None,
    parse_mode: Optional[str] = None,
) -> str:
    """
    Безопасно отправить сообщение в MAX. Возвращает message_id (mid) или пустую строку.
    """
    try:
        result = await api.send_message(chat_id, text, keyboard=keyboard)
        return extract_max_mid(result)
    except Exception:
        logger.exception("safe_send_message_max failed: chat_id=%s", chat_id)
        return ""
