"""
subscription_check.py — проверка подписки пользователя на канал.

Переменные окружения:
  TG_REQUIRED_CHANNEL   — username (@yurta_news) или числовой ID канала TG.
                          Пустая строка → проверка отключена.
  MAX_REQUIRED_CHANNEL_ID — числовой ID группы/канала в MAX.
                          Пустая строка → проверка отключена.

Боту необходимо быть администратором соответствующего канала/группы.
"""

import logging
import os

logger = logging.getLogger(__name__)

TG_REQUIRED_CHANNEL = os.getenv("TG_REQUIRED_CHANNEL", "").strip()
MAX_REQUIRED_CHANNEL_ID = os.getenv("MAX_REQUIRED_CHANNEL_ID", "").strip()


async def is_subscribed_tg(bot, user_id: int) -> bool:
    """Проверяет, подписан ли пользователь на TG-канал.

    Возвращает True если проверка отключена (канал не задан).
    """
    if not TG_REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(
            chat_id=TG_REQUIRED_CHANNEL,
            user_id=user_id,
        )
        return member.status not in ("left", "kicked", "restricted")
    except Exception as e:
        logger.warning("TG subscription check failed for user %s: %s", user_id, e)
        return True  # при ошибке пропускаем (не блокируем пользователя)


async def is_subscribed_max(api, user_id: int) -> bool:
    """Проверяет, подписан ли пользователь на MAX-канал.

    Возвращает True если проверка отключена (канал не задан).
    """
    if not MAX_REQUIRED_CHANNEL_ID:
        return True
    try:
        result = await api._request(
            "GET",
            f"/chats/{MAX_REQUIRED_CHANNEL_ID}/members",
            params={"user_ids": str(user_id)},
        )
        members = result.get("members") or []
        return bool(members)
    except Exception as e:
        logger.warning("MAX subscription check failed for user %s: %s", user_id, e)
        return True  # при ошибке пропускаем
