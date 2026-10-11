from __future__ import annotations
import datetime
import json
from typing import Any, Dict, List, Optional
from db.connection import (
    get_conn, _cur, _maybe_await,
    IntegrityError,
)


# ===========================================================================
# СИСТЕМА УВЕДОМЛЕНИЙ
# ===========================================================================

async def get_notification_type_id(code: str) -> Optional[int]:
    """Получить ID типа уведомления по коду."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT id FROM notification_types WHERE code = %s",
                (code,)
            )
            row = await c.fetchone()
            return int(row["id"]) if row else None


async def get_deadline_type_id(code: str) -> Optional[int]:
    """Получить ID типа срока по коду."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT id FROM deadline_types WHERE code = %s",
                (code,)
            )
            row = await c.fetchone()
            return int(row["id"]) if row else None


async def create_notification(
    type_code: str,
    title: str,
    message: str = None,
    priority: str = None,
    client_id: int = None,
    order_id: int = None,
    chat_id: int = None,
    kkt_id: int = None,
    deadline_type_code: str = None,
    deadline_date: str = None,
    extra_data: dict = None,
    unique_key: str = None,
) -> Optional[int]:
    """
    Создать уведомление.
    Возвращает ID созданного уведомления или None если дубликат.
    """
    type_id = await get_notification_type_id(type_code)
    if not type_id:
        return None

    deadline_type_id = None
    if deadline_type_code:
        deadline_type_id = await get_deadline_type_id(deadline_type_code)

    # Получаем дефолтный приоритет из типа если не указан
    if not priority:
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    "SELECT default_priority FROM notification_types WHERE id = %s",
                    (type_id,)
                )
                row = await c.fetchone()
                priority = row["default_priority"] if row else "medium"

    extra_json = json.dumps(extra_data, ensure_ascii=False) if extra_data else None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            try:
                await c.execute(
                    """
                    INSERT INTO notifications
                        (type_id, priority, title, message, client_id, order_id,
                         chat_id, kkt_id, deadline_type_id, deadline_date, extra_data, unique_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (type_id, priority, title, message, client_id, order_id,
                     chat_id, kkt_id, deadline_type_id, deadline_date, extra_json, unique_key)
                )
                await _maybe_await(conn.commit())
                return c.lastrowid
            except IntegrityError:
                # Дубликат по unique_key
                return None


async def get_notifications(
    unread_only: bool = False,
    type_code: str = None,
    priority: str = None,
    client_id: int = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    Получить список уведомлений с фильтрами.
    """
    conditions = ["n.dismissed_at IS NULL"]
    params = []

    if unread_only:
        conditions.append("n.read_at IS NULL")

    if type_code:
        conditions.append("nt.code = %s")
        params.append(type_code)

    if priority:
        conditions.append("n.priority = %s")
        params.append(priority)

    if client_id:
        conditions.append("n.client_id = %s")
        params.append(client_id)

    where_clause = " AND ".join(conditions)
    params.extend([limit, offset])

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"""
                SELECT
                    n.id, n.priority, n.title, n.message,
                    n.client_id, n.order_id, n.chat_id, n.kkt_id,
                    n.deadline_date, n.extra_data,
                    n.created_at, n.read_at, n.dismissed_at,
                    nt.code AS type_code, nt.name AS type_name, nt.icon AS type_icon,
                    dt.code AS deadline_type_code, dt.name AS deadline_type_name,
                    u.first_name AS client_first_name, u.last_name AS client_last_name,
                    u.inn AS client_inn, u.phone AS client_phone
                FROM notifications n
                JOIN notification_types nt ON nt.id = n.type_id
                LEFT JOIN deadline_types dt ON dt.id = n.deadline_type_id
                LEFT JOIN users u ON u.id = n.client_id
                WHERE {where_clause}
                ORDER BY
                    CASE n.priority
                        WHEN 'urgent' THEN 1
                        WHEN 'high' THEN 2
                        WHEN 'medium' THEN 3
                        WHEN 'low' THEN 4
                    END,
                    n.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params
            )
            return await c.fetchall() or []


async def get_unread_notifications_count() -> int:
    """Получить количество непрочитанных уведомлений."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COUNT(*) AS cnt FROM notifications
                WHERE read_at IS NULL AND dismissed_at IS NULL
                """
            )
            row = await c.fetchone()
            return int(row["cnt"]) if row else 0


async def get_urgent_notifications_count() -> int:
    """Получить количество срочных непрочитанных уведомлений."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COUNT(*) AS cnt FROM notifications
                WHERE read_at IS NULL AND dismissed_at IS NULL
                  AND priority IN ('urgent', 'high')
                """
            )
            row = await c.fetchone()
            return int(row["cnt"]) if row else 0


async def mark_notification_read(notification_id: int, specialist_id: int = None) -> bool:
    """Отметить уведомление как прочитанное."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE notifications
                SET read_at = NOW(), read_by = %s
                WHERE id = %s AND read_at IS NULL
                """,
                (specialist_id, notification_id)
            )
            await _maybe_await(conn.commit())
            return c.rowcount > 0


async def mark_notification_dismissed(notification_id: int, specialist_id: int = None) -> bool:
    """Отклонить уведомление (скрыть навсегда)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE notifications
                SET dismissed_at = NOW(), dismissed_by = %s
                WHERE id = %s AND dismissed_at IS NULL
                """,
                (specialist_id, notification_id)
            )
            await _maybe_await(conn.commit())
            return c.rowcount > 0


async def mark_all_notifications_read(specialist_id: int = None) -> int:
    """Отметить все уведомления как прочитанные. Возвращает количество."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE notifications
                SET read_at = NOW(), read_by = %s
                WHERE read_at IS NULL AND dismissed_at IS NULL
                """,
                (specialist_id,)
            )
            await _maybe_await(conn.commit())
            return c.rowcount


async def get_deadlines_for_calendar(
    year: int,
    month: int,
) -> List[Dict[str, Any]]:
    """
    Получить сроки для календаря за указанный месяц.
    Возвращает список с deadline_date, type, count.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                    n.deadline_date,
                    dt.code AS deadline_type,
                    dt.name AS deadline_type_name,
                    COUNT(*) AS cnt,
                    GROUP_CONCAT(DISTINCT CONCAT(u.first_name, ' ', COALESCE(u.last_name, '')) SEPARATOR ', ') AS clients
                FROM notifications n
                JOIN deadline_types dt ON dt.id = n.deadline_type_id
                LEFT JOIN users u ON u.id = n.client_id
                WHERE n.deadline_date IS NOT NULL
                  AND YEAR(n.deadline_date) = %s
                  AND MONTH(n.deadline_date) = %s
                  AND n.dismissed_at IS NULL
                GROUP BY n.deadline_date, dt.code, dt.name
                ORDER BY n.deadline_date, dt.code
                """,
                (year, month)
            )
            return await c.fetchall() or []


async def check_and_create_deadline_notifications() -> int:
    """
    Проверить все сроки в client_kkt и создать уведомления.
    Вызывается ежедневно cron-задачей.
    Возвращает количество созданных уведомлений.
    """
    today = datetime.date.today()
    created_count = 0

    # Список полей сроков и их типов
    deadline_fields = [
        ('ofd_expires_at', 'ofd', 'ОФД'),
        ('fn_expires_at', 'fn', 'ФН'),
        ('mark_expires_at', 'marking', 'Маркировка'),
        ('tspiot_tariff_expires_at', 'tspiot', 'ТСПИоТ'),
        ('ecp_expires_at', 'ecp', 'ЭЦП'),
    ]

    # Интервалы предупреждений (дней до срока -> тип уведомления)
    warning_intervals = [
        (7, 'deadline_7days'),
        (3, 'deadline_3days'),
        (1, 'deadline_1day'),
        (0, 'deadline_expired'),
        (-1, 'deadline_expired'),  # Для сроков, истёкших вчера
    ]

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            for field_name, deadline_code, deadline_label in deadline_fields:
                # Получаем все ККТ с этим сроком
                await c.execute(
                    f"""
                    SELECT
                        k.id AS kkt_id,
                        k.client_id,
                        k.kkt_slot,
                        k.address,
                        k.{field_name} AS deadline_date,
                        u.first_name, u.last_name, u.inn, u.phone
                    FROM client_kkt k
                    JOIN users u ON u.id = k.client_id
                    WHERE k.{field_name} IS NOT NULL
                      AND k.{field_name} >= %s
                      AND k.{field_name} <= %s
                    """,
                    (today - datetime.timedelta(days=1), today + datetime.timedelta(days=8))
                )
                rows = await c.fetchall() or []

                for row in rows:
                    deadline_date = row["deadline_date"]
                    if isinstance(deadline_date, datetime.datetime):
                        deadline_date = deadline_date.date()

                    days_left = (deadline_date - today).days

                    # Определяем тип уведомления
                    notif_type = None
                    for interval_days, interval_type in warning_intervals:
                        if days_left == interval_days:
                            notif_type = interval_type
                            break

                    if not notif_type:
                        continue

                    # Формируем сообщение
                    client_name = f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
                    client_info = client_name or row['inn'] or row['phone'] or f"ID {row['client_id']}"

                    if days_left > 0:
                        title = f"{deadline_label} истекает через {days_left} дн."
                    elif days_left == 0:
                        title = f"{deadline_label} истекает сегодня!"
                    else:
                        title = f"{deadline_label} истёк!"

                    message = f"Клиент: {client_info}\n"
                    if row['kkt_slot']:
                        message += f"ККТ: {row['kkt_slot']}\n"
                    if row['address']:
                        message += f"Адрес: {row['address']}\n"
                    message += f"Дата: {deadline_date.strftime('%d.%m.%Y')}"

                    # Уникальный ключ для предотвращения дублей
                    unique_key = f"{deadline_code}_{row['kkt_id']}_{deadline_date}_{notif_type}"

                    result = await create_notification(
                        type_code=notif_type,
                        title=title,
                        message=message,
                        client_id=row['client_id'],
                        kkt_id=row['kkt_id'],
                        deadline_type_code=deadline_code,
                        deadline_date=str(deadline_date),
                        unique_key=unique_key,
                    )

                    if result:
                        created_count += 1

    return created_count


async def cleanup_old_notifications(keep_days: int = 30, batch_size: int = 5000) -> int:
    """
    Удаляет уведомления старше keep_days дней батчами.
    notification_delivery_log удаляется каскадно (FK ON DELETE CASCADE).
    Возвращает кол-во удалённых строк.
    """
    total_deleted = 0
    while True:
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    "DELETE FROM notifications WHERE created_at < NOW() - INTERVAL %s DAY LIMIT %s",
                    (keep_days, batch_size),
                )
                deleted = c.rowcount
                await _maybe_await(conn.commit())
        total_deleted += deleted
        if deleted < batch_size:
            break
    return total_deleted
