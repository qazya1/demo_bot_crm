"""Data helpers for dashboard modal windows."""

from django.db import connection

from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)


def _get_all_users(limit=100):
    """Получить всех пользователей"""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, tg_user_id, first_name, last_name, phone, email, tg_username,
                   inn, org_name, city, is_verified, created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT %s
        """, [limit])
        columns = ['user_id', 'tg_user_id', 'first_name', 'last_name', 'phone', 'email',
                   'tg_username', 'inn', 'org_name', 'city', 'verified_at', 'created_at']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_all_orders(limit=100):
    """Получить все заказы"""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT o.id, o.id, o.amount, o.status, o.payment_id,
                   o.created_at, o.paid_at,
                   CONCAT(COALESCE(u.first_name, ''), ' ', COALESCE(u.last_name, '')), u.phone
            FROM orders o
            LEFT JOIN users u ON u.id = o.user_id
            ORDER BY o.created_at DESC
            LIMIT %s
        """, [limit])
        columns = ['id', 'order_id', 'amount', 'status', 'payment_id',
                   'created_at', 'paid_at', 'buyer_name', 'buyer_phone']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_all_chats(limit=100):
    """Получить все чаты"""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT sc.id, sc.client_user_id, sc.specialist_id, sc.status, sc.profession,
                   sc.category, sc.created_at, s.name as specialist_name,
                   u.first_name, u.last_name, u.phone, u.inn,
                   u.tg_user_id, u.max_user_id,
                   (SELECT COUNT(*) FROM support_chat_messages WHERE chat_id = sc.id) as message_count
            FROM support_chats sc
            LEFT JOIN specialists s ON sc.specialist_id = s.id
            LEFT JOIN users u ON u.id = sc.client_user_id
            ORDER BY sc.created_at DESC
            LIMIT %s
        """, [limit])
        columns = ['id', 'client_user_id', 'specialist_id', 'status', 'profession',
                   'category', 'created_at', 'specialist_name', 'client_first_name', 'client_last_name',
                   'client_phone', 'client_inn', 'tg_user_id', 'max_user_id', 'message_count']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_all_specialists():
    """Получить всех специалистов"""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, name, profession, phone, tg_username, role, is_active, created_at
            FROM specialists
            ORDER BY name
        """)
        columns = ['id', 'name', 'profession', 'phone', 'tg_username', 'role', 'is_active', 'created_at']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_all_clients(limit=100):
    """Получить всех клиентов (все пользователи бота)"""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, inn, org_name, first_name, last_name, phone, email, city, kpp
            FROM users
            ORDER BY created_at DESC
            LIMIT %s
        """, [limit])
        columns = ['id', 'inn', 'org_name', 'first_name', 'last_name', 'phone', 'email', 'city', 'kpp']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
