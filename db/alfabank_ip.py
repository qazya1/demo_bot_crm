"""DB-слой для заявок на оформление ИП через API Альфа-Банка.

Таблица создаётся миграцией 029_alfabank_ip_applications.sql,
здесь же есть idempotent ensure-вызов для удобства разработки.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from db.connection import _cur, get_conn

# Допустимые локальные статусы заявки.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DECLINED = "declined"
ALLOWED_STATUSES = {STATUS_PENDING, STATUS_APPROVED, STATUS_DECLINED}


async def ensure_alfabank_ip_schema() -> None:
    """Idempotent создание таблицы (на случай отсутствия миграции).

    Дублирует миграцию 029, чтобы код не падал в окружении, где её не
    применяли руками.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS alfabank_ip_applications (
                    id              INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    user_id         INT NOT NULL,
                    tg_user_id      BIGINT DEFAULT NULL,
                    alfabank_id     VARCHAR(128) DEFAULT NULL,
                    status          ENUM('pending','approved','declined') NOT NULL DEFAULT 'pending',
                    alfabank_state  VARCHAR(64) DEFAULT NULL,
                    alfabank_raw    TEXT DEFAULT NULL,
                    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    KEY idx_alfabank_ip_user (user_id),
                    KEY idx_alfabank_ip_tg (tg_user_id),
                    KEY idx_alfabank_ip_status (status),
                    UNIQUE KEY uq_alfabank_ip_alfabank_id (alfabank_id),
                    CONSTRAINT fk_alfabank_ip_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE ON UPDATE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        await conn.commit()


async def get_active_application(user_id: int) -> Optional[Dict[str, Any]]:
    """Вернуть последнюю заявку пользователя или None.

    "Активная" = последняя по created_at; статус может быть любым, чтобы
    показать пользователю результат предыдущей.
    """
    await ensure_alfabank_ip_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, user_id, tg_user_id, alfabank_id, status,
                       alfabank_state, alfabank_raw, created_at, updated_at
                FROM alfabank_ip_applications
                WHERE user_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (int(user_id),),
            )
            return await c.fetchone()


async def create_application(
    *,
    user_id: int,
    tg_user_id: Optional[int],
    alfabank_id: str,
    status: str = STATUS_PENDING,
    alfabank_state: Optional[str] = None,
    alfabank_raw: Optional[Any] = None,
) -> int:
    """Сохранить новую заявку. Возвращает локальный id."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unknown status: {status}")
    await ensure_alfabank_ip_schema()
    raw_text: Optional[str] = None
    if alfabank_raw is not None:
        try:
            raw_text = json.dumps(alfabank_raw, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_text = str(alfabank_raw)
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO alfabank_ip_applications
                    (user_id, tg_user_id, alfabank_id, status, alfabank_state, alfabank_raw)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    int(user_id),
                    int(tg_user_id) if tg_user_id is not None else None,
                    alfabank_id,
                    status,
                    alfabank_state,
                    raw_text,
                ),
            )
            new_id = c.lastrowid
        await conn.commit()
    return int(new_id)


async def update_application_status(
    *,
    application_id: int,
    status: str,
    alfabank_state: Optional[str] = None,
    alfabank_raw: Optional[Any] = None,
) -> None:
    """Обновить локальный статус и сырой ответ банка."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unknown status: {status}")
    raw_text: Optional[str] = None
    if alfabank_raw is not None:
        try:
            raw_text = json.dumps(alfabank_raw, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_text = str(alfabank_raw)
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE alfabank_ip_applications
                SET status = %s,
                    alfabank_state = COALESCE(%s, alfabank_state),
                    alfabank_raw   = COALESCE(%s, alfabank_raw),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (status, alfabank_state, raw_text, int(application_id)),
            )
        await conn.commit()
