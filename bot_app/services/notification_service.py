"""Notification domain services.

Request-independent notification helpers extracted from
``bot_app.views.notifications``. The view layer keeps decorators, request
parsing, and HTTP response construction while this module owns notification
SQL operations and API payload creation.
"""

from datetime import date, datetime, timedelta

from django.db import connection

from ..permissions import get_current_user
from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)


RENEWAL_NOTIFICATION_CODES = (
    'deadline_expired',
    'deadline_1day',
    'deadline_3days',
    'deadline_7days',
    'deadline_30days',
)


def _get_unread_notif_count() -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) FROM notifications
                WHERE read_at IS NULL AND dismissed_at IS NULL
            """)
            return cursor.fetchone()[0]
    except Exception:
        return 0


def _ensure_notification_tables():
    """Создаёт таблицы уведомлений если не существуют"""
    with connection.cursor() as cursor:
        # 1. notification_types
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notification_types (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    code VARCHAR(50) NOT NULL UNIQUE,
                    name VARCHAR(100) NOT NULL,
                    icon VARCHAR(20) DEFAULT '',
                    default_priority ENUM('low', 'medium', 'high', 'urgent') DEFAULT 'medium',
                    description TEXT DEFAULT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
        except Exception:
            pass

        try:
            cursor.execute("""
                INSERT IGNORE INTO notification_types (code, name, icon, default_priority) VALUES
                    ('deadline_expired', 'Срок истёк', '', 'urgent'),
                    ('deadline_1day', 'Срок завтра', '', 'urgent'),
                    ('deadline_3days', 'Срок через 3 дня', '', 'high'),
                    ('deadline_7days', 'Срок через 7 дней', '', 'medium'),
                    ('deadline_30days', 'Срок через 30 дней', '', 'low'),
                    ('order_new', 'Новый заказ', '', 'medium'),
                    ('order_paid', 'Заказ оплачен', '', 'medium'),
                    ('chat_new', 'Новый чат', '', 'high'),
                    ('client_new', 'Новый клиент', '', 'low')
            """)
        except Exception:
            pass

        # 2. deadline_types
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS deadline_types (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    code VARCHAR(50) NOT NULL UNIQUE,
                    name VARCHAR(100) NOT NULL,
                    description TEXT DEFAULT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
        except Exception:
            pass

        try:
            cursor.execute("""
                INSERT IGNORE INTO deadline_types (code, name) VALUES
                    ('ofd', 'ОФД'), ('fn', 'ФН'), ('marking', 'Маркировка'),
                    ('tspiot', 'ТСПИоТ'), ('ecp', 'ЭЦП'), ('sim', 'SIM')
            """)
        except Exception:
            pass

        # 3. notifications
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    type_id INT NOT NULL,
                    priority ENUM('low', 'medium', 'high', 'urgent') NOT NULL DEFAULT 'medium',
                    title VARCHAR(255) NOT NULL,
                    message TEXT DEFAULT NULL,
                    client_id INT DEFAULT NULL,
                    order_id INT DEFAULT NULL,
                    chat_id BIGINT DEFAULT NULL,
                    kkt_id INT DEFAULT NULL,
                    deadline_type_id INT DEFAULT NULL,
                    deadline_date DATE DEFAULT NULL,
                    extra_data JSON DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    read_at DATETIME DEFAULT NULL,
                    read_by BIGINT DEFAULT NULL,
                    dismissed_at DATETIME DEFAULT NULL,
                    dismissed_by BIGINT DEFAULT NULL,
                    unique_key VARCHAR(255) DEFAULT NULL,
                    INDEX idx_type (type_id),
                    INDEX idx_priority (priority),
                    INDEX idx_client (client_id),
                    INDEX idx_kkt (kkt_id),
                    INDEX idx_deadline_date (deadline_date),
                    INDEX idx_created (created_at),
                    INDEX idx_unread (read_at, dismissed_at),
                    UNIQUE INDEX idx_unique_key (unique_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
        except Exception:
            pass


def _sync_kkt_expiry_notifications():
    """Автоматически создаёт/обновляет уведомления для истекающих сроков ККТ"""
    from ..services.task_service import _ensure_tasks_table, _upsert_task
    from .company_schema import _ensure_client_kkt_company_column

    _ensure_client_kkt_company_column()
    _ensure_tasks_table()
    today = date.today()
    soon = today + timedelta(days=30)

    with connection.cursor() as cursor:
        # Загружаем ID типов уведомлений
        cursor.execute("SELECT id, code FROM notification_types WHERE code LIKE 'deadline_%%'")
        type_map = {row[1]: row[0] for row in cursor.fetchall()}
        if not type_map:
            return

        # Загружаем ID типов сроков
        cursor.execute("SELECT id, code FROM deadline_types")
        dl_type_map = {row[1]: row[0] for row in cursor.fetchall()}

        # ККТ с истекающими/истёкшими сроками
        cursor.execute("""
            SELECT k.id, k.kkt_slot, k.name AS kkt_name,
                   k.fn_expires_at, k.ofd_expires_at, k.mark_expires_at,
                   k.client_id, k.company_id,
                   u.first_name, u.last_name, u.org_name, u.inn,
                   c.name AS company_name
            FROM client_kkt k
            LEFT JOIN users u ON u.id = k.client_id
            LEFT JOIN companies c ON c.id = k.company_id
            WHERE k.fn_expires_at <= %s
               OR k.ofd_expires_at <= %s
               OR k.mark_expires_at <= %s
        """, [soon, soon, soon])

        columns = [col[0] for col in cursor.description]
        kkt_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        deadline_fields = {
            'fn_expires_at': ('ФН', 'fn'),
            'ofd_expires_at': ('ОФД', 'ofd'),
            'mark_expires_at': ('Маркировка', 'marking'),
        }

        for row in kkt_rows:
            owner_name = (row.get('company_name')
                          or row.get('org_name')
                          or f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
                          or f"ИНН {row.get('inn') or '?'}")

            for field, (label, dl_code) in deadline_fields.items():
                exp_date = row.get(field)
                if not exp_date:
                    continue
                if isinstance(exp_date, datetime):
                    exp_date = exp_date.date()
                if exp_date > soon:
                    continue

                days_left = (exp_date - today).days

                # Задачи продления: одна активная задача на каждый тип срока ККТ.
                # Обновляется при каждом открытии CRM/cron, чтобы раздел «Продления» всегда
                # показывал фактический остаток дней, а не только фиксированные точки 30/7/3/1/0.
                if days_left <= 30:
                    if days_left <= 1:
                        prio = 'high'
                    elif days_left <= 7:
                        prio = 'high'
                    else:
                        prio = 'medium'
                    renewal_title = f"Продление {label}: {owner_name} ({days_left} дн.)"
                    renewal_desc = (
                        f"Клиент: {owner_name}\n"
                        f"ККТ слот: {row.get('kkt_slot') or '-'}\n"
                        f"Тип срока: {label}\n"
                        f"Дата окончания: {exp_date.strftime('%d.%m.%Y')}\n"
                        f"До окончания: {days_left} дн."
                    )
                    _upsert_task(
                        title=renewal_title,
                        description=renewal_desc,
                        source_type='renewal',
                        source_id=row['id'],
                        dedupe_key=f"renewal:kkt:{row['id']}:{dl_code}",
                        deadline=datetime.combine(exp_date, datetime.min.time()),
                        priority=prio,
                        meta={
                            'client_id': row.get('client_id'),
                            'company_id': row.get('company_id'),
                            'client_name': owner_name,
                            'kkt_id': row['id'],
                            'kkt_slot': row.get('kkt_slot'),
                            'deadline_kind': dl_code,
                            'days_before': days_left,
                            'expires_at': exp_date.isoformat(),
                        },
                        ensure_schema=False,
                    )

                if days_left < 0:
                    ntype = 'deadline_expired'
                    priority = 'urgent'
                    title = f'{label} ИСТЁК — {owner_name}'
                    message = f'ККТ #{row["kkt_slot"]}: {label} истёк {exp_date.strftime("%d.%m.%Y")} ({abs(days_left)} дн. назад)'
                elif days_left <= 1:
                    ntype = 'deadline_1day'
                    priority = 'urgent'
                    title = f'{label} ИСТЕКАЕТ ЗАВТРА — {owner_name}'
                    message = f'ККТ #{row["kkt_slot"]}: {label} истекает {exp_date.strftime("%d.%m.%Y")}'
                elif days_left <= 3:
                    ntype = 'deadline_3days'
                    priority = 'high'
                    title = f'{label} через {days_left} дн. — {owner_name}'
                    message = f'ККТ #{row["kkt_slot"]}: {label} истекает {exp_date.strftime("%d.%m.%Y")}'
                elif days_left <= 7:
                    ntype = 'deadline_7days'
                    priority = 'medium'
                    title = f'{label} через {days_left} дн. — {owner_name}'
                    message = f'ККТ #{row["kkt_slot"]}: {label} истекает {exp_date.strftime("%d.%m.%Y")}'
                else:
                    ntype = 'deadline_30days'
                    priority = 'low'
                    title = f'{label} через {days_left} дн. — {owner_name}'
                    message = f'ККТ #{row["kkt_slot"]}: {label} истекает {exp_date.strftime("%d.%m.%Y")}'

                type_id = type_map.get(ntype, type_map.get('deadline_7days'))
                dl_type_id = dl_type_map.get(dl_code)
                unique_key = f'kkt_{row["id"]}_{dl_code}_{exp_date.isoformat()}'

                cursor.execute("""
                    INSERT INTO notifications
                        (type_id, priority, title, message, client_id, kkt_id,
                         deadline_type_id, deadline_date, unique_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        priority = VALUES(priority),
                        title = VALUES(title),
                        message = VALUES(message),
                        type_id = VALUES(type_id)
                """, [type_id, priority, title, message,
                      row.get('client_id'), row['id'],
                      dl_type_id, exp_date, unique_key])


def _notifications_page_context(request, *, renewal_only=False):
    """Собирает данные для страниц уведомлений."""
    user = get_current_user(request)

    # Создаём таблицы + синхронизируем сроки ККТ → уведомления
    try:
        _ensure_notification_tables()
        _sync_kkt_expiry_notifications()
    except Exception:
        pass

    filter_type = request.GET.get('type', '')
    filter_priority = request.GET.get('priority', '')
    filter_unread = request.GET.get('unread', '')

    conditions = ["n.dismissed_at IS NULL"]
    params = []

    if filter_unread == '1':
        conditions.append("n.read_at IS NULL")
    if filter_type:
        conditions.append("nt.code = %s")
        params.append(filter_type)
    if filter_priority:
        conditions.append("n.priority = %s")
        params.append(filter_priority)

    renewal_codes = {'deadline_expired', 'deadline_1day', 'deadline_3days', 'deadline_7days', 'deadline_30days'}
    renewal_codes_list = sorted(renewal_codes)
    placeholders = ', '.join(['%s'] * len(renewal_codes_list))
    if renewal_only:
        conditions.append(f"nt.code IN ({placeholders})")
        params.extend(renewal_codes_list)
    else:
        conditions.append(f"nt.code NOT IN ({placeholders})")
        params.extend(renewal_codes_list)

    where_clause = " AND ".join(conditions)

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT
                n.id, n.priority, n.title, n.message,
                n.client_id, n.order_id, n.chat_id, n.kkt_id,
                n.deadline_date, n.extra_data,
                n.created_at, n.read_at,
                nt.code AS type_code, nt.name AS type_name, nt.icon AS type_icon,
                dt.code AS deadline_type_code, dt.name AS deadline_type_name,
                u.first_name AS client_first_name, u.last_name AS client_last_name,
                u.inn AS client_inn
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
            LIMIT 100
        """, params)

        columns = [col[0] for col in cursor.description]
        notifications = [dict(zip(columns, row)) for row in cursor.fetchall()]

        cursor.execute("SELECT code, name, icon FROM notification_types ORDER BY name")
        all_types = [dict(zip(['code', 'name', 'icon'], row)) for row in cursor.fetchall()]
        if renewal_only:
            notification_types = [t for t in all_types if t['code'] in renewal_codes]
        else:
            notification_types = [t for t in all_types if t['code'] not in renewal_codes]

        # ВАЖНО: счётчики "непрочитанных"/"срочных" должны использовать ТОТ ЖЕ
        # фильтр по типам, что и список выше, иначе возникает рассинхрон —
        # счётчик показывает N, а список пуст (например, все непрочитанные
        # попадают в раздел "Продление", а их считают здесь, в "Уведомлениях").
        scope_op = 'IN' if renewal_only else 'NOT IN'
        scope_params = list(renewal_codes_list)

        cursor.execute(f"""
            SELECT COUNT(*) FROM notifications n
            JOIN notification_types nt ON nt.id = n.type_id
            WHERE n.read_at IS NULL AND n.dismissed_at IS NULL
              AND nt.code {scope_op} ({placeholders})
        """, scope_params)
        unread_count = cursor.fetchone()[0]

        cursor.execute(f"""
            SELECT COUNT(*) FROM notifications n
            JOIN notification_types nt ON nt.id = n.type_id
            WHERE n.read_at IS NULL AND n.dismissed_at IS NULL
              AND n.priority IN ('urgent', 'high')
              AND nt.code {scope_op} ({placeholders})
        """, scope_params)
        urgent_count = cursor.fetchone()[0]

    return {
        'user': user,
        'notifications': notifications,
        'notification_types': notification_types,
        'filter_type': filter_type,
        'filter_priority': filter_priority,
        'filter_unread': filter_unread,
        'unread_count': unread_count,
        'urgent_count': urgent_count,
        'renewal_only': renewal_only,
    }

def mark_notification_read(user, notification_id):
    """Отметить уведомление как прочитанное."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE notifications
                SET read_at = NOW(), read_by = %s
                WHERE id = %s AND read_at IS NULL
            """, [user['id'], notification_id])

        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def dismiss_notification(user, notification_id):
    """Отклонить (скрыть) уведомление."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE notifications
                SET dismissed_at = NOW(), dismissed_by = %s
                WHERE id = %s AND dismissed_at IS NULL
            """, [user['id'], notification_id])

        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def mark_notifications_read_all(user, scope):
    """Отметить все уведомления как прочитанные с учётом scope."""
    renewal_codes = RENEWAL_NOTIFICATION_CODES
    try:
        with connection.cursor() as cursor:
            if scope == 'renewal':
                placeholders = ', '.join(['%s'] * len(renewal_codes))
                cursor.execute(f"""
                    UPDATE notifications n
                    JOIN notification_types nt ON nt.id = n.type_id
                    SET n.read_at = NOW(), n.read_by = %s
                    WHERE n.read_at IS NULL AND n.dismissed_at IS NULL
                      AND nt.code IN ({placeholders})
                """, [user['id']] + list(renewal_codes))
            elif scope == 'general':
                placeholders = ', '.join(['%s'] * len(renewal_codes))
                cursor.execute(f"""
                    UPDATE notifications n
                    JOIN notification_types nt ON nt.id = n.type_id
                    SET n.read_at = NOW(), n.read_by = %s
                    WHERE n.read_at IS NULL AND n.dismissed_at IS NULL
                      AND nt.code NOT IN ({placeholders})
                """, [user['id']] + list(renewal_codes))
            else:
                cursor.execute("""
                    UPDATE notifications
                    SET read_at = NOW(), read_by = %s
                    WHERE read_at IS NULL AND dismissed_at IS NULL
                """, [user['id']])
            count = cursor.rowcount

        return {'success': True, 'count': count}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_notifications_count():
    """Получить количество непрочитанных уведомлений для API-бейджа."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN priority IN ('urgent', 'high') THEN 1 ELSE 0 END) AS urgent
                FROM notifications
                WHERE read_at IS NULL AND dismissed_at IS NULL
            """)
            row = cursor.fetchone()

        return {
            'success': True,
            'total': row[0] or 0,
            'urgent': row[1] or 0,
        }
    except Exception as e:
        return {'success': False, 'error': str(e), 'total': 0, 'urgent': 0}
