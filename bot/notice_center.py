"""Единый notice manager для Telegram и MAX.

Модуль вводит инфраструктуру для управления служебными уведомлениями
по единой схеме «одно уведомление на сущность»:

- notice kind: тип уведомления (например, `client_chat`, `specialist_pending`)
- platform: `telegram` или `max`
- user_id: получатель notice
- entity_id: идентификатор сущности notice
  (например, `support_chat_id` для client-chat notice)
- message_id: ID отправленного notice (telegram message_id / max mid)

На этапе 1 модуль предназначен как безопасная in-memory точка консолидации,
без массовой интеграции существующих сценариев.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable


logger = logging.getLogger(__name__)


class NoticePlatform(str, Enum):
    TELEGRAM = "telegram"
    MAX = "max"


class NoticeKind(str, Enum):
    CLIENT_CHAT = "client_chat"
    SPECIALIST_PENDING = "specialist_pending"
    SPECIALIST_CHAT = "specialist_chat"


class NoticeCluster(str, Enum):
    CLIENT_CHAT_NOTICES = "client_chat_notices"
    SPECIALIST_PENDING_NOTICES = "specialist_pending_notices"
    SPECIALIST_CHAT_NOTICES = "specialist_chat_notices"


@dataclass(frozen=True)
class NoticeKey:
    """Уникальный ключ notice в реестре."""

    platform: NoticePlatform
    kind: NoticeKind
    user_id: int
    entity_id: int


@dataclass
class NoticeRecord:
    """Текущее состояние notice в реестре."""

    key: NoticeKey
    message_id: str


class NoticeRegistry:
    """Потокобезопасный in-memory реестр notice.

    Хранит прямой индекс notice по ключу и обратный индекс по кластерам,
    чтобы можно было удалять пачку однотипных notice (cluster delete).
    """

    def __init__(self) -> None:
        self._records: dict[NoticeKey, NoticeRecord] = {}
        self._clusters: dict[tuple[NoticePlatform, NoticeCluster, int], set[NoticeKey]] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, key: NoticeKey, message_id: str) -> NoticeRecord:
        async with self._lock:
            rec = NoticeRecord(key=key, message_id=str(message_id))
            self._records[key] = rec
            self._attach_to_cluster(key)
            return rec

    async def get(self, key: NoticeKey) -> NoticeRecord | None:
        async with self._lock:
            return self._records.get(key)

    async def delete(self, key: NoticeKey) -> NoticeRecord | None:
        async with self._lock:
            rec = self._records.pop(key, None)
            self._detach_from_cluster(key)
            return rec

    async def list_cluster_records(
        self,
        *,
        platform: NoticePlatform,
        cluster: NoticeCluster,
        user_id: int,
    ) -> list[NoticeRecord]:
        async with self._lock:
            keys = self._clusters.get((platform, cluster, int(user_id)), set())
            return [self._records[k] for k in keys if k in self._records]

    def _cluster_for(self, key: NoticeKey) -> NoticeCluster:
        if key.kind == NoticeKind.CLIENT_CHAT:
            return NoticeCluster.CLIENT_CHAT_NOTICES
        if key.kind == NoticeKind.SPECIALIST_PENDING:
            return NoticeCluster.SPECIALIST_PENDING_NOTICES
        if key.kind == NoticeKind.SPECIALIST_CHAT:
            return NoticeCluster.SPECIALIST_CHAT_NOTICES
        raise ValueError(f"Unknown notice kind: {key.kind}")

    def _attach_to_cluster(self, key: NoticeKey) -> None:
        bucket = (key.platform, self._cluster_for(key), key.user_id)
        self._clusters.setdefault(bucket, set()).add(key)

    def _detach_from_cluster(self, key: NoticeKey) -> None:
        bucket = (key.platform, self._cluster_for(key), key.user_id)
        keys = self._clusters.get(bucket)
        if not keys:
            return
        keys.discard(key)
        if not keys:
            self._clusters.pop(bucket, None)


registry = NoticeRegistry()


# ────────────────────────── Telegram helpers ──────────────────────────

async def upsert_notice_tg(
    *,
    tg_bot: Any,
    tg_chat_id: int,
    user_id: int,
    kind: NoticeKind,
    entity_id: int,
    text: str,
    reply_markup: Any = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> str:
    """Создать/обновить служебный notice в Telegram.

    Всегда отправляет новое уведомление, обновляет реестр и удаляет
    предыдущее notice с тем же ключом best-effort.
    """

    key = NoticeKey(
        platform=NoticePlatform.TELEGRAM,
        kind=kind,
        user_id=int(user_id),
        entity_id=int(entity_id),
    )
    prev = await registry.get(key)

    # В уведомлениях с inline-кнопками не редактируем старое сообщение: при
    # новом событии из той же сущности отправляем свежее уведомление и удаляем
    # предыдущее. Так кнопки всегда относятся к последнему notice, а старые
    # уведомления не остаются висеть в истории чата.
    sent = await tg_bot.send_message(
        chat_id=tg_chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
    )
    message_id = str(getattr(sent, "message_id", "") or "")
    if not message_id:
        raise RuntimeError("upsert_notice_tg: empty message_id from Telegram API")
    await registry.upsert(key, message_id)

    if prev and prev.message_id != message_id:
        try:
            await tg_bot.delete_message(chat_id=tg_chat_id, message_id=int(prev.message_id))
        except Exception:
            logger.debug("upsert_notice_tg failed to delete previous notice", exc_info=True)

    return message_id


async def delete_notice_tg(*, tg_bot: Any, tg_chat_id: int, user_id: int, kind: NoticeKind, entity_id: int) -> None:
    """Удалить одно notice Telegram и убрать его из реестра."""

    key = NoticeKey(
        platform=NoticePlatform.TELEGRAM,
        kind=kind,
        user_id=int(user_id),
        entity_id=int(entity_id),
    )
    rec = await registry.delete(key)
    if not rec:
        return
    try:
        await tg_bot.delete_message(chat_id=tg_chat_id, message_id=int(rec.message_id))
    except Exception:
        logger.debug("delete_notice_tg failed", exc_info=True)


async def delete_notice_cluster_tg(
    *,
    tg_bot: Any,
    tg_chat_id: int,
    user_id: int,
    cluster: NoticeCluster,
) -> None:
    """Удалить все notice Telegram в рамках кластера пользователя."""

    records = await registry.list_cluster_records(
        platform=NoticePlatform.TELEGRAM,
        cluster=cluster,
        user_id=int(user_id),
    )
    for rec in records:
        await delete_notice_tg(
            tg_bot=tg_bot,
            tg_chat_id=tg_chat_id,
            user_id=rec.key.user_id,
            kind=rec.key.kind,
            entity_id=rec.key.entity_id,
        )


async def clear_other_notices_for_user_tg(
    *,
    tg_bot: Any,
    tg_chat_id: int,
    user_id: int,
    cluster: NoticeCluster,
    current_message_id: int | str | None,
) -> bool:
    """Удалить все notice кластера пользователя, кроме текущего.

    Возвращает True только если текущий `message_id` найден в notice-кластере
    (т.е. callback пришёл именно из служебного notice).
    """

    cur_mid = str(current_message_id or "").strip()
    if not cur_mid:
        return False

    records = await registry.list_cluster_records(
        platform=NoticePlatform.TELEGRAM,
        cluster=cluster,
        user_id=int(user_id),
    )
    if not records:
        return False

    if not any(str(rec.message_id) == cur_mid for rec in records):
        return False

    for rec in records:
        if str(rec.message_id) == cur_mid:
            continue
        try:
            await delete_notice_tg(
                tg_bot=tg_bot,
                tg_chat_id=int(tg_chat_id),
                user_id=rec.key.user_id,
                kind=rec.key.kind,
                entity_id=rec.key.entity_id,
            )
        except Exception:
            logger.debug("clear_other_notices_for_user_tg failed", exc_info=True)
    return True


# ───────────────────────────── MAX helpers ────────────────────────────


def _extract_max_mid(send_result: dict[str, Any]) -> str:
    msg_obj = send_result.get("message")
    if not isinstance(msg_obj, dict):
        return ""
    return str(msg_obj.get("body", {}).get("mid") or "").strip()


def _is_max_delete_forbidden(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    if int(result.get("http_status") or 0) != 403:
        return False

    text = str(result.get("message") or result.get("error") or result)
    return "Insufficient permissions to delete message" in text or "403" in text


async def upsert_notice_max(
    *,
    max_api: Any,
    max_user_id: int,
    kind: NoticeKind,
    entity_id: int,
    text: str,
    keyboard: Any = None,
    parse_mode: str | None = None,
) -> str:
    """Создать/обновить служебный notice в MAX.

    Всегда отправляет новое уведомление, обновляет реестр и удаляет
    предыдущее notice с тем же ключом best-effort.
    """

    key = NoticeKey(
        platform=NoticePlatform.MAX,
        kind=kind,
        user_id=int(max_user_id),
        entity_id=int(entity_id),
    )
    prev = await registry.get(key)

    # Не редактируем старый MAX-notice: новое событие из той же сущности
    # должно заменить уведомление через send + delete previous.

    send_result = await max_api.send_message_to_user(
        user_id=max_user_id,
        text=text,
        keyboard=keyboard,
        parse_mode=parse_mode,
    )
    if not bool((send_result or {}).get("ok")):
        raise RuntimeError(f"upsert_notice_max send failed: user_id={max_user_id} kind={kind.value} result={send_result}")
    new_mid = _extract_max_mid(send_result)
    if not new_mid:
        raise RuntimeError("upsert_notice_max: empty mid from MAX send_message_to_user")

    await registry.upsert(key, new_mid)

    if prev and prev.message_id != new_mid:
        try:
            delete_result = await max_api.delete_message(prev.message_id)
            if not bool((delete_result or {}).get("ok")):
                if _is_max_delete_forbidden(delete_result):
                    logger.debug(
                        "MAX notice fallback delete forbidden, ignored: user_id=%s message_id=%s kind=%s",
                        max_user_id,
                        prev.message_id,
                        kind.value,
                    )
                else:
                    logger.warning(
                        "MAX notice fallback delete failed: user_id=%s message_id=%s kind=%s result=%s",
                        max_user_id,
                        prev.message_id,
                        kind.value,
                        delete_result,
                    )
        except Exception:
            logger.debug("upsert_notice_max failed to delete previous notice", exc_info=True)

    return new_mid


async def delete_notice_max(*, max_api: Any, max_user_id: int, kind: NoticeKind, entity_id: int) -> None:
    """Удалить одно notice MAX и убрать его из реестра."""

    key = NoticeKey(
        platform=NoticePlatform.MAX,
        kind=kind,
        user_id=int(max_user_id),
        entity_id=int(entity_id),
    )
    rec = await registry.delete(key)
    if not rec:
        return
    try:
        result = await max_api.delete_message(rec.message_id)
        if not bool((result or {}).get("ok")):
            if _is_max_delete_forbidden(result):
                logger.debug(
                    "MAX notice delete forbidden, ignored: user_id=%s message_id=%s kind=%s",
                    max_user_id,
                    rec.message_id,
                    kind.value,
                )
            else:
                logger.warning(
                    "MAX notice delete failed: user_id=%s message_id=%s kind=%s result=%s",
                    max_user_id,
                    rec.message_id,
                    kind.value,
                    result,
                )
    except Exception:
        logger.debug("delete_notice_max failed", exc_info=True)


async def delete_notice_cluster_max(
    *,
    max_api: Any,
    max_user_id: int,
    cluster: NoticeCluster,
) -> None:
    """Удалить все notice MAX в рамках кластера пользователя."""

    records = await registry.list_cluster_records(
        platform=NoticePlatform.MAX,
        cluster=cluster,
        user_id=int(max_user_id),
    )
    for rec in records:
        await delete_notice_max(
            max_api=max_api,
            max_user_id=rec.key.user_id,
            kind=rec.key.kind,
            entity_id=rec.key.entity_id,
        )


async def clear_other_notices_for_user_max(
    *,
    max_api: Any,
    max_user_id: int,
    cluster: NoticeCluster,
    current_message_id: int | str | None,
) -> bool:
    """Удалить все MAX-notice кластера пользователя, кроме текущего."""

    cur_mid = str(current_message_id or "").strip()
    if not cur_mid:
        return False

    records = await registry.list_cluster_records(
        platform=NoticePlatform.MAX,
        cluster=cluster,
        user_id=int(max_user_id),
    )
    if not records:
        return False

    if not any(str(rec.message_id) == cur_mid for rec in records):
        return False

    for rec in records:
        if str(rec.message_id) == cur_mid:
            continue
        try:
            await delete_notice_max(
                max_api=max_api,
                max_user_id=rec.key.user_id,
                kind=rec.key.kind,
                entity_id=rec.key.entity_id,
            )
        except Exception:
            logger.debug("clear_other_notices_for_user_max failed", exc_info=True)
    return True


async def clear_registry() -> None:
    """Тестовый/служебный хелпер: очистить in-memory реестр notice."""

    # Используем delete по существующим ключам, чтобы корректно почистить кластеры.
    all_keys = list(registry._records.keys())
    for key in all_keys:
        await registry.delete(key)


async def clear_user_notices_tg(*, tg_bot: Any, tg_chat_id: int, user_id: int) -> None:
    """Best-effort очистка всех notice-кластеров Telegram пользователя."""
    for cluster in NoticeCluster:
        try:
            await delete_notice_cluster_tg(
                tg_bot=tg_bot,
                tg_chat_id=int(tg_chat_id),
                user_id=int(user_id),
                cluster=cluster,
            )
        except Exception:
            logger.debug("clear_user_notices_tg failed: user_id=%s cluster=%s", user_id, cluster.value, exc_info=True)


async def clear_user_notices_max(*, max_api: Any, max_user_id: int) -> None:
    """Best-effort очистка всех notice-кластеров MAX пользователя."""
    for cluster in NoticeCluster:
        try:
            await delete_notice_cluster_max(
                max_api=max_api,
                max_user_id=int(max_user_id),
                cluster=cluster,
            )
        except Exception:
            logger.debug("clear_user_notices_max failed: user_id=%s cluster=%s", max_user_id, cluster.value, exc_info=True)


# Типы-фабрики для будущей интеграции (опционально, документируют интерфейсы)
TGDeleteFn = Callable[..., Awaitable[Any]]
TGEditFn = Callable[..., Awaitable[Any]]
TGSendFn = Callable[..., Awaitable[Any]]
