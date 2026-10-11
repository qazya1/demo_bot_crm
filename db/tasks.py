from __future__ import annotations
from typing import Any, Dict, List, Optional
from db.connection import get_conn, _cur, _maybe_await


# ===========================================================================
# ЗАДАЧИ (CRM Tasks)
# ===========================================================================

async def ensure_tasks_table():
    """Создаёт таблицу tasks, если её ещё нет."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
                    title       VARCHAR(500)   NOT NULL,
                    description TEXT           NULL,
                    source_type VARCHAR(32)    NOT NULL DEFAULT 'manual',
                    source_id   BIGINT         NULL,
                    created_by  BIGINT         NOT NULL,
                    assignee_id BIGINT         NULL,
                    assigned_at DATETIME       NULL,
                    completed_at DATETIME      NULL,
                    deadline    DATETIME       NULL,
                    priority    VARCHAR(16)    NOT NULL DEFAULT 'medium',
                    status      VARCHAR(32)    NOT NULL DEFAULT 'awaiting_assignment',
                    result_comment TEXT        NULL,
                    created_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_tasks_assignee  (assignee_id),
                    INDEX idx_tasks_status    (status),
                    INDEX idx_tasks_deadline  (deadline),
                    INDEX idx_tasks_source    (source_type, source_id),
                    INDEX idx_tasks_created_by(created_by)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            await c.execute("SHOW COLUMNS FROM tasks LIKE 'assigned_at'")
            if not await c.fetchone():
                await c.execute("ALTER TABLE tasks ADD COLUMN assigned_at DATETIME NULL AFTER assignee_id")
            await c.execute("SHOW COLUMNS FROM tasks LIKE 'completed_at'")
            if not await c.fetchone():
                await c.execute("ALTER TABLE tasks ADD COLUMN completed_at DATETIME NULL AFTER assigned_at")
            await _maybe_await(conn.commit())


async def create_task(
    *,
    title: str,
    description: str | None = None,
    source_type: str = "manual",
    source_id: int | None = None,
    created_by: int,
    assignee_id: int | None = None,
    deadline: str | None = None,
    priority: str = "medium",
) -> int | None:
    """Создаёт задачу. Возвращает id."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO tasks
                  (title, description, source_type, source_id,
                   created_by, assignee_id, deadline, priority, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'awaiting_assignment')
                """,
                (title, description, source_type, source_id,
                 created_by, assignee_id, deadline, priority),
            )
            await _maybe_await(conn.commit())
            return int(c.lastrowid)


async def update_task(
    task_id: int,
    **fields,
) -> bool:
    """Обновляет произвольные поля задачи."""
    allowed = {
        "title", "description", "assignee_id", "deadline",
        "priority", "status", "result_comment", "completed_at",
    }
    to_set = {k: v for k, v in fields.items() if k in allowed}
    if not to_set:
        return False

    # Каноничная фиксация времени фактического завершения:
    # - при переходе в done заполняем completed_at (если ещё пусто);
    # - при выходе из done очищаем completed_at.
    if "status" in to_set:
        new_status = (to_set.get("status") or "").strip().lower()
        if new_status == "done":
            to_set.setdefault("completed_at", "__SET_NOW_IF_NULL__")
        elif new_status:
            to_set["completed_at"] = None

    set_parts: list[str] = []
    values: list[Any] = []
    for field, value in to_set.items():
        if value == "__SET_NOW_IF_NULL__":
            set_parts.append("completed_at = COALESCE(completed_at, NOW())")
            continue
        set_parts.append(f"{field} = %s")
        values.append(value)
    set_clause = ", ".join(set_parts)
    values.append(task_id)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = %s",
                values,
            )
            await _maybe_await(conn.commit())
            return c.rowcount == 1


async def get_task(task_id: int) -> Dict[str, Any] | None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT t.*,
                       cr.name AS creator_name,
                       asg.name AS assignee_name
                FROM tasks t
                LEFT JOIN specialists cr  ON cr.id  = t.created_by
                LEFT JOIN specialists asg ON asg.id = t.assignee_id
                WHERE t.id = %s
                """,
                (task_id,),
            )
            return await c.fetchone()


async def get_tasks_for_director(
    *,
    status: str | None = None,
    assignee_id: int | None = None,
    source_type: str | None = None,
    priority: str | None = None,
    overdue_only: bool = False,
    limit: int = 300,
) -> List[Dict[str, Any]]:
    """Все задачи (для директора) с фильтрами."""
    conditions = []
    params: list = []

    if status:
        conditions.append("t.status = %s")
        params.append(status)
    if assignee_id:
        conditions.append("t.assignee_id = %s")
        params.append(assignee_id)
    if source_type:
        conditions.append("t.source_type = %s")
        params.append(source_type)
    if priority:
        conditions.append("t.priority = %s")
        params.append(priority)
    if overdue_only:
        conditions.append("t.deadline < NOW() AND t.status NOT IN ('done')")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"""
                SELECT t.*,
                       cr.name AS creator_name,
                       asg.name AS assignee_name,
                       CASE WHEN t.deadline < NOW() AND t.status NOT IN ('done')
                            THEN 1 ELSE 0
                       END AS is_overdue
                FROM tasks t
                LEFT JOIN specialists cr  ON cr.id  = t.created_by
                LEFT JOIN specialists asg ON asg.id = t.assignee_id
                {where}
                ORDER BY
                    CASE WHEN t.status IN ('awaiting_assignment', 'in_progress') THEN 0 ELSE 1 END ASC,
                    t.created_at DESC
                LIMIT %s
                """,
                params + [limit],
            )
            return await c.fetchall() or []


async def get_tasks_for_specialist(
    specialist_id: int,
    *,
    status: str | None = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Задачи конкретного специалиста."""
    conditions = ["t.assignee_id = %s"]
    params: list = [specialist_id]

    if status:
        conditions.append("t.status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(conditions)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"""
                SELECT t.*,
                       cr.name AS creator_name,
                       asg.name AS assignee_name,
                       CASE WHEN t.deadline < NOW() AND t.status NOT IN ('done')
                            THEN 1 ELSE 0
                       END AS is_overdue
                FROM tasks t
                LEFT JOIN specialists cr  ON cr.id  = t.created_by
                LEFT JOIN specialists asg ON asg.id = t.assignee_id
                {where}
                ORDER BY
                    CASE WHEN t.status IN ('awaiting_assignment', 'in_progress') THEN 0 ELSE 1 END ASC,
                    t.created_at DESC
                LIMIT %s
                """,
                params + [limit],
            )
            return await c.fetchall() or []


async def get_task_stats_director() -> Dict[str, int]:
    """Статистика задач (для шапки директора)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("""
                SELECT
                    COUNT(*)                                                        AS total,
                    SUM(status = 'in_progress')                                     AS cnt_in_progress,
                    SUM(status = 'awaiting_assignment')                             AS cnt_awaiting,
                    SUM(status = 'done')                                            AS cnt_done,
                    SUM(deadline < NOW() AND status NOT IN ('done'))                AS cnt_overdue
                FROM tasks
            """)
            row = await c.fetchone()
            if not row:
                return {"total": 0, "awaiting": 0, "in_progress": 0, "done": 0, "overdue": 0}
            return {
                "total":       int(row["total"] or 0),
                "awaiting":    int(row["cnt_awaiting"] or 0),
                "in_progress": int(row["cnt_in_progress"] or 0),
                "done":        int(row["cnt_done"] or 0),
                "overdue":     int(row["cnt_overdue"] or 0),
            }


async def get_task_stats_specialist(specialist_id: int) -> Dict[str, int]:
    """Статистика задач для конкретного специалиста."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("""
                SELECT
                    COUNT(*)                                                        AS total,
                    SUM(status = 'in_progress')                                     AS cnt_in_progress,
                    SUM(status = 'awaiting_assignment')                             AS cnt_awaiting,
                    SUM(status = 'done')                                            AS cnt_done,
                    SUM(deadline < NOW() AND status NOT IN ('done'))                AS cnt_overdue
                FROM tasks
                WHERE assignee_id = %s
            """, (specialist_id,))
            row = await c.fetchone()
            if not row:
                return {"total": 0, "awaiting": 0, "in_progress": 0, "done": 0, "overdue": 0}
            return {
                "total":       int(row["total"] or 0),
                "awaiting":    int(row["cnt_awaiting"] or 0),
                "in_progress": int(row["cnt_in_progress"] or 0),
                "done":        int(row["cnt_done"] or 0),
                "overdue":     int(row["cnt_overdue"] or 0),
            }


async def delete_task(task_id: int) -> bool:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            await _maybe_await(conn.commit())
            return c.rowcount == 1
