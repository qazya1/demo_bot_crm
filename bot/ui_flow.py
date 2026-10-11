"""
ui_flow.py — единый слой lifecycle-управления сообщениями для Telegram и MAX.

Архитектура:
- 4 типа сообщений: screen, notice, user_input, payment_screen
- MessagePolicy — централизованные TTL-константы
- TelegramUIFlow / MaxUIFlow — платформенные адаптеры с одинаковым интерфейсом
- Все бизнес-модули работают через эти адаптеры, а не через прямые send/edit/delete
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Типы сообщений
# ══════════════════════════════════════════════════════════════════════

class MessageType(str, Enum):
    SCREEN = "screen"
    NOTICE = "notice"
    USER_INPUT = "user_input"
    PAYMENT_SCREEN = "payment_screen"


# ══════════════════════════════════════════════════════════════════════
# Ключи состояния в user_data
# ══════════════════════════════════════════════════════════════════════

ACTIVE_SCREEN_ID_KEY = "_active_screen_id"
PAYMENT_SCREEN_ID_KEY = "_payment_screen_id"
NOTICE_IDS_KEY = "_notice_ids"
TRACKED_USER_INPUT_IDS_KEY = "_tracked_user_input_ids"
PROTECTED_MESSAGE_IDS_KEY = "_protected_message_ids"

# Legacy-ключи (для обратной совместимости при миграции)
TG_LAST_MENU_KEY = "_menu_msg_id"
MAX_LAST_MENU_KEY = "_last_menu_mid"


# ══════════════════════════════════════════════════════════════════════
# Политика TTL
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MessagePolicy:
    notice_ttl_sec: int = 5
    pii_ttl_sec: int = 5
    user_input_ttl_sec: int = 5


DEFAULT_POLICY = MessagePolicy()
FAST_NOTICE_POLICY = MessagePolicy(notice_ttl_sec=5)


# ══════════════════════════════════════════════════════════════════════
# Утилиты работы с user_data
# ══════════════════════════════════════════════════════════════════════

def _get_list(user_data: dict, key: str) -> list:
    val = user_data.get(key)
    if not isinstance(val, list):
        user_data[key] = []
        return user_data[key]
    return val


def _append_id(user_data: dict, key: str, message_id: int | str) -> None:
    arr = _get_list(user_data, key)
    mid = str(message_id)
    if mid not in arr:
        arr.append(mid)
    # ограничиваем размер
    if len(arr) > 60:
        user_data[key] = arr[-60:]


def _clear_list(user_data: dict, key: str) -> None:
    user_data[key] = []


# ══════════════════════════════════════════════════════════════════════
# Telegram UI Flow
# ══════════════════════════════════════════════════════════════════════

class TelegramUIFlow:
    """
    Единый интерфейс управления сообщениями для Telegram.

    Бизнес-код вызывает методы этого класса вместо прямых
    send_message / edit_message_text / delete_message.
    """

    def __init__(self, policy: MessagePolicy = DEFAULT_POLICY):
        self.policy = policy

    async def render_screen(
        self,
        update,
        context,
        text: str,
        *,
        reply_markup=None,
        parse_mode=None,
        force_new: bool = False,
        allow_media_replace: bool = True,
    ) -> int:
        from telegram.error import BadRequest
        from message_cleanup import safe_edit_message_tg, safe_delete_message_tg

        chat = update.effective_chat
        if not chat:
            return 0
        chat_id = int(chat.id)
        user_data = context.user_data

        payment_mid = user_data.get(PAYMENT_SCREEN_ID_KEY)
        current_screen_id = user_data.get(ACTIVE_SCREEN_ID_KEY)
        if not current_screen_id:
            current_screen_id = user_data.get(TG_LAST_MENU_KEY)

        q = update.callback_query
        callback_mid = int(q.message.message_id) if (q and q.message) else None

        new_mid: int | None = None

        if q and not force_new and callback_mid:
            if payment_mid and str(callback_mid) == str(payment_mid):
                return await self.render_notice(update, context, text, reply_markup=reply_markup, parse_mode=parse_mode)

            ok = await safe_edit_message_tg(
                context, chat_id, callback_mid,
                text=text, reply_markup=reply_markup, parse_mode=parse_mode,
            )
            if ok:
                new_mid = callback_mid

        if new_mid is None and current_screen_id and not force_new:
            ok = await safe_edit_message_tg(
                context, chat_id, int(current_screen_id),
                text=text, reply_markup=reply_markup, parse_mode=parse_mode,
            )
            if ok:
                new_mid = int(current_screen_id)

        if new_mid is None:
            if current_screen_id:
                asyncio.create_task(safe_delete_message_tg(context, chat_id, int(current_screen_id)))

            try:
                msg = await chat.send_message(
                    text=text, reply_markup=reply_markup, parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                new_mid = int(msg.message_id)
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    return int(current_screen_id) if current_screen_id else 0
                raise

        user_data[ACTIVE_SCREEN_ID_KEY] = new_mid
        user_data[TG_LAST_MENU_KEY] = new_mid

        old_ids = set()
        if current_screen_id:
            old_ids.add(int(current_screen_id))
        if callback_mid:
            old_ids.add(int(callback_mid))
        old_ids.discard(new_mid)

        for old_id in old_ids:
            asyncio.create_task(safe_delete_message_tg(context, chat_id, old_id))

        return new_mid

    async def render_notice(
        self,
        update,
        context,
        text: str,
        *,
        reply_markup=None,
        parse_mode=None,
        ttl_sec: int | None = None,
    ) -> int:
        chat = update.effective_chat
        if not chat:
            return 0
        user_data = context.user_data

        ttl = ttl_sec if ttl_sec is not None else self.policy.notice_ttl_sec

        try:
            msg = await chat.send_message(
                text=text, reply_markup=reply_markup, parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            mid = int(msg.message_id)
        except Exception:
            logger.exception("render_notice failed: chat_id=%s", int(chat.id))
            return 0

        _append_id(user_data, NOTICE_IDS_KEY, mid)
        asyncio.create_task(self._schedule_delete_tg(context, int(chat.id), mid, ttl))

        await self._cleanup_stale_notices(context, int(chat.id))

        return mid

    async def track_user_input(
        self,
        update,
        context,
        *,
        ttl_sec: int | None = None,
    ) -> None:
        msg = update.effective_message
        if not msg:
            return
        mid = int(msg.message_id)
        user_data = context.user_data

        text = msg.text or ""
        is_pii = self._text_contains_pii(text)
        ttl = ttl_sec if ttl_sec is not None else (
            self.policy.pii_ttl_sec if is_pii else self.policy.user_input_ttl_sec
        )

        _append_id(user_data, TRACKED_USER_INPUT_IDS_KEY, mid)
        asyncio.create_task(self._schedule_delete_tg(context, int(msg.chat_id), mid, ttl))

    async def render_payment_screen(
        self,
        update,
        context,
        text: str,
        *,
        reply_markup=None,
        parse_mode=None,
    ) -> int:
        from message_cleanup import safe_edit_message_tg, safe_delete_message_tg

        chat = update.effective_chat
        if not chat:
            return 0
        chat_id = int(chat.id)
        user_data = context.user_data

        payment_mid = user_data.get(PAYMENT_SCREEN_ID_KEY)

        new_mid: int | None = None

        if payment_mid:
            ok = await safe_edit_message_tg(
                context, chat_id, int(payment_mid),
                text=text, reply_markup=reply_markup, parse_mode=parse_mode,
            )
            if ok:
                new_mid = int(payment_mid)

        if new_mid is None:
            if payment_mid:
                asyncio.create_task(safe_delete_message_tg(context, chat_id, int(payment_mid)))

            try:
                msg = await chat.send_message(
                    text=text, reply_markup=reply_markup, parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                new_mid = int(msg.message_id)
            except Exception:
                logger.exception("render_payment_screen failed: chat_id=%s", chat_id)
                return 0

        user_data[PAYMENT_SCREEN_ID_KEY] = new_mid
        return new_mid

    async def clear_payment_screen(
        self,
        context,
        chat_id: int,
    ) -> None:
        from message_cleanup import safe_delete_message_tg

        user_data = context.user_data
        payment_mid = user_data.get(PAYMENT_SCREEN_ID_KEY)
        if payment_mid:
            await safe_delete_message_tg(context, int(chat_id), int(payment_mid))
            user_data.pop(PAYMENT_SCREEN_ID_KEY, None)

    async def cleanup_notices(self, context, chat_id: int) -> None:
        from message_cleanup import safe_delete_message_tg

        user_data = context.user_data
        notice_ids = _get_list(user_data, NOTICE_IDS_KEY)
        for mid in list(notice_ids):
            await safe_delete_message_tg(context, int(chat_id), int(mid))
        _clear_list(user_data, NOTICE_IDS_KEY)

    async def cleanup_tracked_user_inputs(self, context, chat_id: int) -> None:
        from message_cleanup import safe_delete_message_tg

        user_data = context.user_data
        input_ids = _get_list(user_data, TRACKED_USER_INPUT_IDS_KEY)
        for mid in list(input_ids):
            await safe_delete_message_tg(context, int(chat_id), int(mid))
        _clear_list(user_data, TRACKED_USER_INPUT_IDS_KEY)

    async def _schedule_delete_tg(self, context, chat_id: int, message_id: int, ttl_sec: int) -> None:
        from message_cleanup import safe_delete_message_tg

        await asyncio.sleep(ttl_sec)
        await safe_delete_message_tg(context, chat_id, message_id)

    async def _cleanup_stale_notices(self, context, chat_id: int) -> None:
        from message_cleanup import safe_delete_message_tg

        user_data = context.user_data
        notice_ids = _get_list(user_data, NOTICE_IDS_KEY)
        if len(notice_ids) > 30:
            to_remove = notice_ids[:len(notice_ids) - 20]
            for mid in to_remove:
                await safe_delete_message_tg(context, int(chat_id), int(mid))
            user_data[NOTICE_IDS_KEY] = notice_ids[len(to_remove):]

    @staticmethod
    def _text_contains_pii(text: str) -> bool:
        import re
        if not text:
            return False
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 10:
            return True
        if "@" in text and "." in text:
            return True
        return False


class MaxUIFlow:
    """
    Единый интерфейс управления сообщениями для MAX.

    Для MAX допустимо: удалить старый screen -> отправить новый.
    Бизнес-коду дан тот же интерфейс, что и для Telegram.
    """

    def __init__(self, policy: MessagePolicy = DEFAULT_POLICY):
        self.policy = policy

    @staticmethod
    def _resolve_chat_id(ctx, chat_id: str | None) -> str:
        cid = str(chat_id or "").strip()
        if cid:
            return cid
        stored = str((ctx.user_data or {}).get("_chat_id") or "").strip()
        return stored

    async def render_screen(
        self,
        ctx,
        chat_id: str,
        text: str,
        *,
        reply_markup=None,
        parse_mode=None,
        force_new: bool = False,
        allow_media_replace: bool = True,
        message_id: str | None = None,
    ) -> str:
        from message_cleanup import max_safe_delete_message

        user_data = ctx.user_data
        resolved_chat_id = self._resolve_chat_id(ctx, chat_id)

        if not resolved_chat_id:
            logger.error("MaxUIFlow.render_screen: empty recipient chat_id, text=%r", text[:120])
            return ""

        payment_mid = user_data.get(PAYMENT_SCREEN_ID_KEY)
        current_screen_id = user_data.get(ACTIVE_SCREEN_ID_KEY)
        if not current_screen_id:
            current_screen_id = user_data.get(MAX_LAST_MENU_KEY)

        if payment_mid and (message_id and str(message_id) == str(payment_mid)):
            return await self.render_notice(ctx, resolved_chat_id, text, keyboard=reply_markup)

        old_mid = message_id or current_screen_id
        if old_mid and not force_new:
            asyncio.create_task(max_safe_delete_message(ctx.api, str(old_mid)))

        result = await ctx.api.send_message(resolved_chat_id, text, keyboard=reply_markup)
        new_mid = self._extract_mid(result)

        if new_mid:
            user_data[ACTIVE_SCREEN_ID_KEY] = new_mid
            user_data[MAX_LAST_MENU_KEY] = new_mid

        return new_mid or ""

    async def render_notice(
        self,
        ctx,
        chat_id: str,
        text: str,
        *,
        keyboard=None,
        parse_mode=None,
        ttl_sec: int | None = None,
    ) -> str:
        ttl = ttl_sec if ttl_sec is not None else self.policy.notice_ttl_sec
        resolved_chat_id = self._resolve_chat_id(ctx, chat_id)

        if not resolved_chat_id:
            logger.error("MaxUIFlow.render_notice: empty recipient chat_id, text=%r", text[:120])
            return ""

        result = await ctx.api.send_message(resolved_chat_id, text, keyboard=keyboard)
        mid = self._extract_mid(result)

        if mid:
            user_data = ctx.user_data
            _append_id(user_data, NOTICE_IDS_KEY, mid)
            asyncio.create_task(self._schedule_delete_max(ctx, resolved_chat_id, mid, ttl))

        return mid or ""

    async def track_user_input(
        self,
        ctx,
        chat_id: str,
        message_id: str,
        *,
        ttl_sec: int | None = None,
    ) -> None:
        # MAX API does not support deleting user messages — only bot messages can
        # be deleted. Tracking user inputs for cleanup is therefore a no-op here.
        return

    async def render_payment_screen(
        self,
        ctx,
        chat_id: str,
        text: str,
        *,
        reply_markup=None,
        parse_mode=None,
    ) -> str:
        from message_cleanup import max_safe_delete_message

        user_data = ctx.user_data
        resolved_chat_id = self._resolve_chat_id(ctx, chat_id)

        if not resolved_chat_id:
            logger.error("MaxUIFlow.render_payment_screen: empty recipient chat_id, text=%r", text[:120])
            return ""

        payment_mid = user_data.get(PAYMENT_SCREEN_ID_KEY)

        if payment_mid:
            asyncio.create_task(max_safe_delete_message(ctx.api, str(payment_mid)))

        result = await ctx.api.send_message(resolved_chat_id, text, keyboard=reply_markup)
        new_mid = self._extract_mid(result)

        if new_mid:
            user_data[PAYMENT_SCREEN_ID_KEY] = new_mid

        return new_mid or ""

    async def clear_payment_screen(
        self,
        ctx,
        chat_id: str,
    ) -> None:
        from message_cleanup import max_safe_delete_message

        user_data = ctx.user_data
        payment_mid = user_data.get(PAYMENT_SCREEN_ID_KEY)
        if payment_mid:
            await max_safe_delete_message(ctx.api, str(payment_mid))
            user_data.pop(PAYMENT_SCREEN_ID_KEY, None)

    async def cleanup_notices(self, ctx, chat_id: str) -> None:
        from message_cleanup import max_safe_delete_message

        user_data = ctx.user_data
        notice_ids = _get_list(user_data, NOTICE_IDS_KEY)
        for mid in list(notice_ids):
            await max_safe_delete_message(ctx.api, str(mid))
        _clear_list(user_data, NOTICE_IDS_KEY)

    async def cleanup_tracked_user_inputs(self, ctx, chat_id: str) -> None:
        from message_cleanup import max_safe_delete_message

        user_data = ctx.user_data
        input_ids = _get_list(user_data, TRACKED_USER_INPUT_IDS_KEY)
        for mid in list(input_ids):
            await max_safe_delete_message(ctx.api, str(mid))
        _clear_list(user_data, TRACKED_USER_INPUT_IDS_KEY)

    async def _schedule_delete_max(self, ctx, chat_id: str, message_id: str, ttl_sec: int) -> None:
        from message_cleanup import max_safe_delete_message

        await asyncio.sleep(ttl_sec)
        await max_safe_delete_message(ctx.api, str(message_id))

    @staticmethod
    def _extract_mid(result: dict) -> str:
        if not isinstance(result, dict):
            return ""
        msg_obj = result.get("message")
        if not isinstance(msg_obj, dict):
            return ""
        return str(msg_obj.get("body", {}).get("mid") or "").strip()

    @staticmethod
    def _text_contains_pii(text: str) -> bool:
        import re
        if not text:
            return False
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 10:
            return True
        if "@" in text and "." in text:
            return True
        return False
