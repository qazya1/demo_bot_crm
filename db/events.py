from __future__ import annotations
import json
from typing import Any, Dict, Optional
from db.connection import get_conn, _cur, _maybe_await


# ====== Analytics events ======

async def log_event(
    *,
    event_type: str,
    tg_user_id: int | None = None,
    max_user_id: int | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    update_id: int | None = None,
    name: str | None = None,
    callback_data: str | None = None,
    text_len: int | None = None,
    has_file: bool | None = None,
    payload: dict | None = None,
    internal_user_id: int | None = None,
) -> None:
    """
    Пишет событие в bot_events.
    Важно: ошибки логирования не должны валить бота.
    Если передан internal_user_id — используется напрямую (для MAX-бота).
    """
    if not internal_user_id:
        from db.users import resolve_internal_user_id

        internal_user_id = await resolve_internal_user_id(
            internal_user_id=internal_user_id,
            tg_user_id=tg_user_id,
            max_user_id=max_user_id,
            create=True,
        )

    try:
        payload_json: Optional[str] = None
        if payload is not None:
            payload_json = json.dumps(payload, ensure_ascii=False)

        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    """
                    INSERT INTO bot_events
                      (tg_user_id, chat_id, message_id, update_id,
                       event_type, name, callback_data, text_len, has_file, payload, user_id)
                    VALUES
                      (%s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tg_user_id,
                        chat_id,
                        message_id,
                        update_id,
                        str(event_type),
                        name,
                        callback_data,
                        text_len,
                        (1 if has_file else 0) if has_file is not None else None,
                        payload_json,  # MySQL сам приведёт строку к JSON, если колонка JSON
                        internal_user_id
                    ),
                )
                await _maybe_await(conn.commit())
    except Exception:
        return

async def aggregate_and_cleanup_bot_events(keep_days: int = 7, batch_size: int = 5000) -> int:
    """
    Агрегирует события старше keep_days дней в bot_events_weekly,
    затем удаляет их из bot_events батчами. Возвращает кол-во удалённых строк.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO bot_events_weekly
                    (week_start, event_type, name,
                     cnt_total, cnt_with_file, avg_text_len,
                     uniq_users, first_ts, last_ts)
                SELECT
                    DATE_SUB(DATE(ts), INTERVAL WEEKDAY(ts) DAY) AS week_start,
                    event_type,
                    COALESCE(name, '')                           AS name,
                    COUNT(*)                                     AS cnt_total,
                    SUM(COALESCE(has_file, 0))                   AS cnt_with_file,
                    AVG(text_len)                                AS avg_text_len,
                    COUNT(DISTINCT tg_user_id)                   AS uniq_users,
                    MIN(ts)                                      AS first_ts,
                    MAX(ts)                                      AS last_ts
                FROM bot_events
                WHERE ts < NOW() - INTERVAL %s DAY
                GROUP BY week_start, event_type, COALESCE(name, '')
                ON DUPLICATE KEY UPDATE
                    cnt_total     = VALUES(cnt_total),
                    cnt_with_file = VALUES(cnt_with_file),
                    avg_text_len  = VALUES(avg_text_len),
                    uniq_users    = VALUES(uniq_users),
                    first_ts      = LEAST(first_ts, VALUES(first_ts)),
                    last_ts       = GREATEST(last_ts, VALUES(last_ts))
                """,
                (keep_days,),
            )
            await _maybe_await(conn.commit())

    total_deleted = 0
    while True:
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    "DELETE FROM bot_events WHERE ts < NOW() - INTERVAL %s DAY LIMIT %s",
                    (keep_days, batch_size),
                )
                deleted = c.rowcount
                await _maybe_await(conn.commit())
        total_deleted += deleted
        if deleted < batch_size:
            break

    return total_deleted


async def ui_get_main_menu_mid(tg_user_id: int) -> int | None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT main_menu_mid FROM ui_state WHERE tg_user_id=%s",
                (tg_user_id,),
            )
            row = await c.fetchone()
            if row and row.get("main_menu_mid"):
                return int(row["main_menu_mid"])
            return None


async def ui_set_main_menu_mid(tg_user_id: int, mid: int | None) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO ui_state (tg_user_id, main_menu_mid)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE main_menu_mid=VALUES(main_menu_mid)
                """,
                (tg_user_id, mid),
            )
            await _maybe_await(conn.commit())
