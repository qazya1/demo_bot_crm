"""Director dashboard and analytics views."""

import os
from datetime import date, timedelta

from django.db import connection
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie

from ..permissions import director_required, get_current_user, login_required_custom
from ..sql_cursor import with_sql_rewrite
from ..sql_helpers import _column_exists
from .dashboard_helpers import (
    _get_all_chats,
    _get_all_clients,
    _get_all_orders,
    _get_all_specialists,
    _get_all_users,
)
from .notifications import _ensure_notification_tables, _sync_kkt_expiry_notifications

connection = with_sql_rewrite(connection)


def _get_dashboard_modal_helpers():
    """Return dashboard modal helpers extracted from legacy views."""
    return _get_all_users, _get_all_orders, _get_all_chats, _get_all_specialists, _get_all_clients


def _ensure_client_kkt_company_column_lazy():
    """Lazy-load client KKT schema helper."""
    from .company_schema import _ensure_client_kkt_company_column

    return _ensure_client_kkt_company_column()


# Словарь переводов для действий бота
ACTION_TRANSLATIONS = {
    'buy': 'Покупка',
    'back_to_main': 'Назад в меню',
    'refresh': 'Обновить',
    'command': 'Команда',
    'services_ofd': 'Услуги ОФД',
    'ag_refresh': 'Обновить данные',
    'text': 'Текстовое сообщение',
    'cc_menu': 'Меню кассы',
    'start': 'Запуск бота',
    'help': 'Помощь',
    'profile': 'Профиль',
    'settings': 'Настройки',
    'support': 'Поддержка',
    'orders': 'Заказы',
    'services': 'Услуги',
    'menu': 'Главное меню',
    'cancel': 'Отмена',
    'confirm': 'Подтвердить',
    'back': 'Назад',
    'next': 'Далее',
    'prev': 'Предыдущий',
    'select': 'Выбрать',
    'Выберите раздел:': 'Выбор раздела',
    '? Ваши чаты / заявки:': 'Чаты и заявки',
}

# Словарь переводов для типов событий
EVENT_TYPE_TRANSLATIONS = {
    'callback': 'Нажатие кнопки',
    'callback_query': 'Нажатие кнопки',
    'screen': 'Просмотр экрана',
    'message': 'Сообщение',
    'command': 'Команда',
    'inline_query': 'Inline запрос',
    'text': 'Текст',
}


@ensure_csrf_cookie
@login_required_custom
@director_required
def dashboard(request):
    """Главный дашборд (только директор)"""
    user = get_current_user(request)
    stats = _get_overall_stats()
    financial = _get_financial_stats()
    top_buttons = _get_top_buttons()
    activity = _get_activity_timeline()
    top_users = _get_top_users()
    specialists_stats = _get_specialists_stats()
    expiring = _get_expiring()

    # Создаём таблицы уведомлений + синхронизируем сроки ККТ
    try:
        _ensure_notification_tables()
        _sync_kkt_expiry_notifications()
    except Exception:
        pass

    # Данные для модальных окон
    (
        _get_all_users,
        _get_all_orders,
        _get_all_chats,
        _get_all_specialists,
        _get_all_clients,
    ) = _get_dashboard_modal_helpers()
    all_users = _get_all_users(limit=100)
    all_orders = _get_all_orders(limit=100)
    all_chats = _get_all_chats(limit=100)
    all_specialists = _get_all_specialists()
    all_clients = _get_all_clients(limit=100)

    # Количество непрочитанных уведомлений (для бейджа в сайдбаре)
    unread_notif_count = 0
    dashboard_notifications = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) FROM notifications
                WHERE read_at IS NULL AND dismissed_at IS NULL
            """)
            unread_notif_count = cursor.fetchone()[0]

            cursor.execute("""
                SELECT n.id, n.priority, n.title, n.message,
                       n.client_id, n.kkt_id, n.deadline_date,
                       nt.icon AS type_icon
                FROM notifications n
                JOIN notification_types nt ON nt.id = n.type_id
                WHERE n.dismissed_at IS NULL AND n.read_at IS NULL
                ORDER BY
                    CASE n.priority
                        WHEN 'urgent' THEN 1 WHEN 'high' THEN 2
                        WHEN 'medium' THEN 3 ELSE 4
                    END,
                    n.created_at DESC
                LIMIT 10
            """)
            columns = [col[0] for col in cursor.description]
            dashboard_notifications = [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception:
        pass

    return render(request, 'bot_app/dashboard.html', {
        'stats': stats,
        'financial': financial,
        'top_buttons': top_buttons,
        'activity': activity,
        'top_users': top_users,
        'specialists_stats': specialists_stats,
        'expiring': expiring,
        'today': date.today(),
        'all_users': all_users,
        'all_orders': all_orders,
        'all_chats': all_chats,
        'all_specialists': all_specialists,
        'all_clients': all_clients,
        'user': user,
        'dashboard_notifications': dashboard_notifications,
        'unread_notif_count': unread_notif_count,
    })


@login_required_custom
@director_required
def analytics(request):
    """Аналитика (только директор) - дашборд по платформам MAX / Telegram"""
    user = get_current_user(request)
    period_map = {
        'all': None,
        '7d': 7,
        '30d': 30,
        '90d': 90,
    }
    period_key = (request.GET.get('period') or '30d').strip().lower()
    if period_key not in period_map:
        period_key = '30d'

    # Получаем данные из сервиса аналитики
    import asyncio
    from services.analytics_service import get_analytics_summary, get_revenue_breakdown
    
    # Запускаем асинхронные функции аналитики
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        analytics_data = loop.run_until_complete(get_analytics_summary())
        revenue_data = loop.run_until_complete(
            get_revenue_breakdown(
                period_days=period_map.get(period_key),
            )
        )
    finally:
        loop.close()
    
    # Реферальная статистика с пользователями инлайн
    referral_stats = _get_referral_stats()
    # Загружаем пользователей для каждого кода
    for ref in referral_stats:
        ref['users'] = _get_users_by_referral(ref['id'])

    return render(request, 'bot_app/analytics.html', {
        'analytics': analytics_data,
        'revenue': revenue_data,
        'revenue_filters': {
            'period': period_key,
        },
        'referral_stats': referral_stats,
        'user': user,
        'tg_bot_username': 'SellerCrm_bot',
        'max_bot_username': os.getenv('MAX_BOT_NAME', 'id344222933834_bot'),
    })


# ==================== СТАТИСТИКА ====================

def _get_overall_stats():
    with connection.cursor() as cursor:
        orders_has_source = _column_exists('orders', 'source')
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        # Верифицирован = заполнены ИНН + телефон
        cursor.execute("""
            SELECT COUNT(*)
            FROM users
            WHERE inn IS NOT NULL AND TRIM(inn) != ''
              AND phone IS NOT NULL AND TRIM(phone) != ''
        """)
        verified_users = cursor.fetchone()[0]
        # В CRM считаем клиентами всех пользователей бота (включая тех, кто только нажал /start)
        cursor.execute("SELECT COUNT(*) FROM users")
        total_clients = cursor.fetchone()[0]
        # Активировался = хотя бы раз зашёл в бот (/start), т.е. есть tg_user_id или max_user_id
        cursor.execute("SELECT COUNT(*) FROM users WHERE tg_user_id IS NOT NULL OR max_user_id IS NOT NULL")
        activated_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE tg_user_id IS NOT NULL")
        tg_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE max_user_id IS NOT NULL")
        max_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders")
        total_orders = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders WHERE paid_at IS NOT NULL")
        paid_orders = cursor.fetchone()[0]
        if orders_has_source:
            cursor.execute("SELECT COUNT(*) FROM orders WHERE source = 'telegram'")
            tg_orders = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM orders WHERE source = 'max'")
            max_orders = cursor.fetchone()[0]
        else:
            tg_orders = 0
            max_orders = 0
        cursor.execute("SELECT COUNT(*) FROM support_chats WHERE request_type IS NULL OR request_type != 'order'")
        total_chats = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM support_chats WHERE status IN ('pending', 'active', 'consultation') AND (request_type IS NULL OR request_type != 'order')")
        active_chats = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM specialists WHERE is_active = 1")
        active_specialists = cursor.fetchone()[0]

    return {
        'total_users': total_users,
        'verified_users': verified_users,
        'total_clients': total_clients,
        'activated_users': activated_users,
        'tg_users': tg_users,
        'max_users': max_users,
        'total_orders': total_orders,
        'paid_orders': paid_orders,
        'tg_orders': tg_orders,
        'max_orders': max_orders,
        'total_chats': total_chats,
        'active_chats': active_chats,
        'active_specialists': active_specialists,
    }


def _get_financial_stats():
    """
    Расчёт финансовой статистики за 30 дней:
    - Выручка 30д = сумма оплаченных заказов за 30 дней
    - Себестоимость 30д = сумма source_price кодов из оплаченных заказов за 30 дней
    - Прибыль 30д = Выручка 30д - Себестоимость 30д
    - Средний чек 30д = средняя сумма оплаченного заказа за 30 дней
    """
    with connection.cursor() as cursor:
        date_filter = "paid_at IS NOT NULL AND paid_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)"

        # Общая выручка (все оплаченные заказы за всё время)
        cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE paid_at IS NOT NULL")
        total_revenue = cursor.fetchone()[0] or 0

        # Выручка за 30 дней
        cursor.execute(f"""
            SELECT COALESCE(SUM(amount), 0)
            FROM orders
            WHERE {date_filter}
        """)
        revenue_30d = cursor.fetchone()[0] or 0

        # Себестоимость за 30 дней (source_price кодов из оплаченных заказов за 30 дней)
        cursor.execute(f"""
            SELECT COALESCE(SUM(c.source_price), 0)
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            LEFT JOIN codes c ON c.id = oi.code_id
            WHERE o.{date_filter}
        """)
        cost_30d = cursor.fetchone()[0] or 0

        # Прибыль за 30 дней
        profit_30d = revenue_30d - cost_30d

        # Средний чек за 30 дней
        cursor.execute(f"""
            SELECT AVG(amount) FROM orders WHERE {date_filter}
        """)
        avg_order_30d = cursor.fetchone()[0] or 0

    return {
        'total_revenue': total_revenue,
        'revenue_30d': revenue_30d,
        'profit': profit_30d,
        'avg_order': round(avg_order_30d, 2),
    }


def _get_top_buttons():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                name,
                callback_data,
                COUNT(*) as click_count
            FROM bot_events
            WHERE event_type = 'callback_query' AND name IS NOT NULL
            GROUP BY name, callback_data
            ORDER BY click_count DESC
            LIMIT 20
        """)
        columns = ['name', 'callback_data', 'click_count']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_activity_timeline():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                DATE(ts) as date,
                COUNT(*) as events_count,
                COUNT(DISTINCT user_id) as unique_users
            FROM bot_events
            WHERE ts >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY DATE(ts)
            ORDER BY date DESC
            LIMIT 30
        """)
        columns = ['date', 'events_count', 'unique_users']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_top_users():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                u.id,
                u.tg_user_id,
                u.first_name,
                u.last_name,
                u.tg_username,
                COUNT(be.id) as events_count
            FROM users u
            LEFT JOIN bot_events be ON be.user_id = u.tg_user_id
            GROUP BY u.id, u.tg_user_id, u.first_name, u.last_name, u.tg_username
            HAVING events_count > 0
            ORDER BY events_count DESC
            LIMIT 10
        """)
        columns = ['id', 'tg_user_id', 'first_name', 'last_name', 'tg_username', 'events_count']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_specialists_stats():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                s.id,
                s.name,
                s.profession,
                COUNT(DISTINCT sc.id) as total_chats,
                COUNT(DISTINCT CASE WHEN sc.status = 'closed' THEN sc.id END) as closed_chats,
                COALESCE((
                    SELECT SUM(o.amount)
                    FROM orders o
                    WHERE o.status IN ('CONFIRMED', 'COMPLETED')
                      AND o.paid_at IS NOT NULL
                      AND (
                          o.specialist_id = s.id
                          OR EXISTS (
                              SELECT 1
                              FROM support_chat_payments scp2
                              WHERE scp2.payment_id = o.payment_id
                                AND scp2.specialist_id = s.id
                          )
                      )
                ), 0) as total_revenue
            FROM specialists s
            LEFT JOIN support_chats sc ON sc.specialist_id = s.id
            WHERE s.is_active = 1
            GROUP BY s.id, s.name, s.profession
            ORDER BY total_chats DESC
        """)
        columns = ['id', 'name', 'profession', 'total_chats', 'closed_chats', 'total_revenue']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_expiring():
    _ensure_client_kkt_company_column_lazy()
    today = date.today()
    soon = today + timedelta(days=30)

    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM client_kkt WHERE fn_expires_at BETWEEN %s AND %s", [today, soon])
        fn_expiring = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM client_kkt WHERE fn_expires_at < %s", [today])
        fn_expired = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM client_kkt WHERE mark_expires_at BETWEEN %s AND %s", [today, soon])
        mark_expiring = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM client_kkt WHERE mark_expires_at < %s", [today])
        mark_expired = cursor.fetchone()[0]

        # Детализация: конкретные ККТ с истекающими/истёкшими сроками + клиент
        cursor.execute("""
            SELECT k.id, k.kkt_slot, k.name AS kkt_name, k.address,
                   k.fn_expires_at, k.ofd_expires_at, k.mark_expires_at,
                   k.client_id, k.company_id,
                   u.first_name, u.last_name, u.org_name, u.inn, u.phone
            FROM client_kkt k
            LEFT JOIN users u ON u.id = k.client_id
            WHERE k.fn_expires_at < %s
               OR k.fn_expires_at BETWEEN %s AND %s
               OR k.mark_expires_at < %s
               OR k.mark_expires_at BETWEEN %s AND %s
               OR k.ofd_expires_at < %s
               OR k.ofd_expires_at BETWEEN %s AND %s
            ORDER BY
                LEAST(
                    COALESCE(k.fn_expires_at, '2099-12-31'),
                    COALESCE(k.mark_expires_at, '2099-12-31'),
                    COALESCE(k.ofd_expires_at, '2099-12-31')
                ) ASC
            LIMIT 30
        """, [today, today, soon, today, today, soon, today, today, soon])
        columns = [col[0] for col in cursor.description]
        expiring_details = [dict(zip(columns, row)) for row in cursor.fetchall()]

    return {
        'fn_expiring': fn_expiring,
        'fn_expired': fn_expired,
        'mark_expiring': mark_expiring,
        'mark_expired': mark_expired,
        'details': expiring_details,
    }






def _get_conversion_funnel():
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM users")
        all_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_verified = 1")
        verified = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT user_id) FROM orders")
        with_orders = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT user_id) FROM orders WHERE paid_at IS NOT NULL")
        paid = cursor.fetchone()[0]

    return {
        'all_users': all_users,
        'verified': verified,
        'with_orders': with_orders,
        'paid': paid,
        'conversion_verified': round(verified / all_users * 100, 1) if all_users > 0 else 0,
        'conversion_orders': round(with_orders / verified * 100, 1) if verified > 0 else 0,
        'conversion_paid': round(paid / with_orders * 100, 1) if with_orders > 0 else 0,
    }


def _get_events_by_type():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT event_type, COUNT(*) as count
            FROM bot_events
            GROUP BY event_type
            ORDER BY count DESC
        """)
        results = []
        for row in cursor.fetchall():
            event_type = row[0]
            translated = EVENT_TYPE_TRANSLATIONS.get(event_type, event_type)
            results.append({'type': translated, 'count': row[1]})
        return results


def _get_popular_actions():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT name, COUNT(*) as count
            FROM bot_events
            WHERE name IS NOT NULL
            GROUP BY name
            ORDER BY count DESC
            LIMIT 15
        """)
        results = []
        for row in cursor.fetchall():
            action_name = row[0]
            translated = ACTION_TRANSLATIONS.get(action_name, action_name)
            results.append({'action': translated, 'count': row[1]})
        return results


# ==================== РЕФЕРАЛЬНАЯ СТАТИСТИКА ====================

def _has_source_platform_column():
    """Проверяет наличие колонки source_platform в referral_activations."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'referral_activations'
                  AND COLUMN_NAME = 'source_platform'
            """)
            return cursor.fetchone()[0] > 0
    except Exception:
        return False


def _get_referral_stats():
    """Статистика по всем реферальным кодам с разбивкой по платформам"""
    has_sp = _has_source_platform_column()
    code_type_sql = "COALESCE(NULLIF(rc.code_type, ''), 'manual')"

    with connection.cursor() as cursor:
        if has_sp:
            cursor.execute("""
                SELECT
                    rc.id,
                    rc.code,
                    rc.owner_name,
                    rc.owner_note,
                    """ + code_type_sql + """ as code_type,
                    rc.is_active,
                    rc.created_at,
                    (SELECT COUNT(*) FROM referral_activations ra WHERE ra.referral_code_id = rc.id) as activations_count,
                    (SELECT COUNT(DISTINCT ra.user_id) FROM referral_activations ra WHERE ra.referral_code_id = rc.id) as users_count,
                    (SELECT COUNT(DISTINCT ra.user_id) FROM referral_activations ra WHERE ra.referral_code_id = rc.id AND ra.source_platform = 'telegram') as tg_count,
                    (SELECT COUNT(DISTINCT ra.user_id) FROM referral_activations ra WHERE ra.referral_code_id = rc.id AND ra.source_platform = 'max') as max_count
                FROM referral_codes rc
                ORDER BY users_count DESC, rc.created_at DESC
            """)
            columns = ['id', 'code', 'owner_name', 'owner_note', 'code_type', 'is_active', 'created_at',
                       'activations_count', 'users_count', 'tg_count', 'max_count']
        else:
            # Fallback: определяем платформу по наличию tg_user_id / max_user_id в users
            cursor.execute("""
                SELECT
                    rc.id,
                    rc.code,
                    rc.owner_name,
                    rc.owner_note,
                    """ + code_type_sql + """ as code_type,
                    rc.is_active,
                    rc.created_at,
                    (SELECT COUNT(*) FROM referral_activations ra WHERE ra.referral_code_id = rc.id) as activations_count,
                    (SELECT COUNT(DISTINCT ra.user_id) FROM referral_activations ra WHERE ra.referral_code_id = rc.id) as users_count,
                    (SELECT COUNT(DISTINCT ra.user_id)
                       FROM referral_activations ra
                       JOIN users u ON CAST(u.id AS CHAR) = ra.user_id COLLATE utf8mb4_unicode_ci
                      WHERE ra.referral_code_id = rc.id AND u.tg_user_id IS NOT NULL) as tg_count,
                    (SELECT COUNT(DISTINCT ra.user_id)
                       FROM referral_activations ra
                       JOIN users u ON CAST(u.id AS CHAR) = ra.user_id COLLATE utf8mb4_unicode_ci
                      WHERE ra.referral_code_id = rc.id AND u.max_user_id IS NOT NULL) as max_count
                FROM referral_codes rc
                ORDER BY users_count DESC, rc.created_at DESC
            """)
            columns = ['id', 'code', 'owner_name', 'owner_note', 'code_type', 'is_active', 'created_at',
                       'activations_count', 'users_count', 'tg_count', 'max_count']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _get_users_by_referral(code_id, platform_filter=None):
    """Пользователи по реферальному коду с информацией о платформе"""
    has_sp = _has_source_platform_column()
    with connection.cursor() as cursor:
        sp_select = "ra.source_platform" if has_sp else "NULL as source_platform"
        sql = f"""
            SELECT
                u.id,
                u.tg_user_id,
                u.max_user_id,
                u.first_name,
                u.last_name,
                u.phone,
                u.email,
                u.tg_username,
                u.inn,
                u.org_name,
                u.city,
                u.is_verified,
                u.created_at,
                ra.activated_at,
                ra.raw_text,
                {sp_select}
            FROM users u
            JOIN referral_activations ra
                ON ra.user_id COLLATE utf8mb4_unicode_ci = CAST(u.id AS CHAR) COLLATE utf8mb4_unicode_ci
               AND ra.referral_code_id = %s
            WHERE ra.referral_code_id = %s
        """
        params = [code_id, code_id]
        if platform_filter in ('telegram', 'max'):
            if has_sp:
                sql += " AND ra.source_platform = %s"
                params.append(platform_filter)
            else:
                # Fallback: фильтр по наличию tg_user_id / max_user_id
                if platform_filter == 'telegram':
                    sql += " AND u.tg_user_id IS NOT NULL"
                else:
                    sql += " AND u.max_user_id IS NOT NULL"
        sql += " ORDER BY COALESCE(ra.activated_at, u.created_at) DESC"
        cursor.execute(sql, params)
        columns = ['id', 'tg_user_id', 'max_user_id', 'first_name', 'last_name', 'phone', 'email', 'tg_username',
                   'inn', 'org_name', 'city', 'is_verified', 'created_at', 'activated_at', 'raw_text', 'source_platform']
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        # Определяем платформу для записей без source_platform
        for u in results:
            if not u.get('source_platform'):
                if u.get('max_user_id'):
                    u['source_platform'] = 'max'
                elif u.get('tg_user_id'):
                    u['source_platform'] = 'telegram'
                else:
                    u['source_platform'] = None
        return results

