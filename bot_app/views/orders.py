"""CRM order views extracted from the legacy views module."""

import calendar
import json
from datetime import date, datetime

from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from ..permissions import (
    director_required,
    get_current_user,
    login_required_custom,
    tech_or_director_required,
)
from ..sql_cursor import with_sql_rewrite
from .dashboard_helpers import _get_all_specialists
from .settings import _ensure_external_catalog_tables
from ..services.order_service import (
    create_manual_deal,
    create_order_client,
    delete_order,
    search_order_clients_and_companies,
    update_order,
    _ensure_orders_manual_columns,
)

connection = with_sql_rewrite(connection)


def _get_current_month_period():
    today = date.today()
    first_day = today.replace(day=1)
    last_day = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    return first_day, last_day


def _parse_stats_period(date_from_raw, date_to_raw):
    default_from, default_to = _get_current_month_period()
    try:
        date_from = datetime.strptime(date_from_raw, "%Y-%m-%d").date() if date_from_raw else default_from
    except Exception:
        date_from = default_from
    try:
        date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").date() if date_to_raw else default_to
    except Exception:
        date_to = default_to
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _get_orders_stats(user, date_from, date_to, source_filter="", specialist_filter=""):
    is_full_access = user.get('has_crm_access')
    conditions = ["DATE(o.created_at) >= %s", "DATE(o.created_at) <= %s"]
    params = [date_from.isoformat(), date_to.isoformat()]
    specialist_scope_sql = (
        "(o.specialist_id = %s "
        "OR (o.source = 'manual' AND o.author_name = %s) "
        "OR EXISTS (SELECT 1 FROM support_chat_payments scp WHERE scp.payment_id = o.payment_id AND scp.specialist_id = %s))"
    )

    if source_filter:
        conditions.append("o.source = %s")
        params.append(source_filter)
    if is_full_access and specialist_filter:
        conditions.append("o.specialist_id = %s")
        params.append(specialist_filter)
    if not is_full_access:
        conditions.append(specialist_scope_sql)
        params.extend([user['id'], user['name'], user['id']])

    where_sql = "WHERE " + " AND ".join(conditions)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN o.status = 'CONFIRMED' THEN 1 ELSE 0 END) AS paid,
                SUM(CASE WHEN o.status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN o.status = 'ADVANCE' THEN 1 ELSE 0 END) AS advance,
                SUM(CASE WHEN o.status = 'INIT' THEN 1 ELSE 0 END) AS unpaid,
                SUM(CASE WHEN o.status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN o.status IN ('CONFIRMED', 'COMPLETED') THEN COALESCE(o.amount, 0) ELSE 0 END) AS revenue,
                SUM(CASE WHEN o.status = 'ADVANCE' THEN COALESCE(o.amount, 0) ELSE 0 END) AS advance_sum
            FROM orders o
            {where_sql}
            """,
            params,
        )
        row = cursor.fetchone()

    return {
        "total": int(row[0] or 0),
        "paid": int(row[1] or 0),
        "completed": int(row[2] or 0),
        "advance": int(row[3] or 0),
        "unpaid": int(row[4] or 0),
        "cancelled": int(row[5] or 0),
        "revenue": float(row[6] or 0),
        "advance_sum": float(row[7] or 0),
    }


def _get_orders_filters_and_scope(request, user):
    is_full_access = user.get('has_crm_access')
    source_filter = request.GET.get('source', '').strip()
    status_filter = request.GET.get('status', '').strip()
    specialist_filter = request.GET.get('specialist_id', '').strip()
    q = request.GET.get('q', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    paid_date_from = request.GET.get('paid_date_from', '').strip()
    paid_date_to = request.GET.get('paid_date_to', '').strip()
    amount_from = request.GET.get('amount_from', '').strip()
    amount_to = request.GET.get('amount_to', '').strip()

    conditions = []
    params = []
    specialist_scope_sql = (
        "(o.specialist_id = %s "
        "OR (o.source = 'manual' AND o.author_name = %s) "
        "OR EXISTS (SELECT 1 FROM support_chat_payments scp WHERE scp.payment_id = o.payment_id AND scp.specialist_id = %s))"
    )
    if not is_full_access:
        conditions.append(specialist_scope_sql)
        params.extend([user['id'], user['name'], user['id']])

    if source_filter:
        conditions.append("o.source = %s")
        params.append(source_filter)
    if status_filter == 'paid':
        conditions.append("o.status = 'CONFIRMED'")
    elif status_filter == 'advance':
        conditions.append("o.status = 'ADVANCE'")
    elif status_filter == 'completed':
        conditions.append("o.status = 'COMPLETED'")
    elif status_filter == 'unpaid':
        conditions.append("o.status = 'INIT'")
    elif status_filter == 'cancelled':
        conditions.append("o.status = 'CANCELLED'")
    elif status_filter:
        conditions.append("o.status = %s")
        params.append(status_filter)

    if is_full_access and specialist_filter:
        conditions.append("o.specialist_id = %s")
        params.append(specialist_filter)

    if q:
        like_q = f"%{q}%"
        conditions.append(
            "("
            "CAST(o.id AS CHAR) LIKE %s OR o.description LIKE %s OR COALESCE(items.item_names, '') LIKE %s "
            "OR COALESCE(u.first_name, '') LIKE %s OR COALESCE(u.last_name, '') LIKE %s "
            "OR COALESCE(c.name, '') LIKE %s OR COALESCE(u.phone, '') LIKE %s "
            "OR COALESCE(u.inn, '') LIKE %s OR COALESCE(u.email, '') LIKE %s OR COALESCE(sp.name, '') LIKE %s"
            ")"
        )
        params.extend([like_q] * 10)

    if date_from:
        conditions.append("DATE(o.created_at) >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(o.created_at) <= %s")
        params.append(date_to)
    if paid_date_from:
        conditions.append("DATE(o.paid_at) >= %s")
        params.append(paid_date_from)
    if paid_date_to:
        conditions.append("DATE(o.paid_at) <= %s")
        params.append(paid_date_to)
    if amount_from:
        conditions.append("o.amount >= %s")
        params.append(amount_from)
    if amount_to:
        conditions.append("o.amount <= %s")
        params.append(amount_to)

    return {
        'where': ("WHERE " + " AND ".join(conditions)) if conditions else "",
        'params': params,
        'source_filter': source_filter,
        'status_filter': status_filter,
        'specialist_filter': specialist_filter,
        'q': q,
        'date_from': date_from,
        'date_to': date_to,
        'paid_date_from': paid_date_from,
        'paid_date_to': paid_date_to,
        'amount_from': amount_from,
        'amount_to': amount_to,
        'is_full_access': is_full_access,
        'specialist_scope_sql': specialist_scope_sql,
    }


def _ensure_address_columns(cursor=None):
    """Добавляет колонку address в таблицы users и orders, если её ещё нет."""
    if cursor is None:
        with connection.cursor() as own_cursor:
            _ensure_address_columns(own_cursor)
        return
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN address TEXT DEFAULT NULL")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN address TEXT DEFAULT NULL")
    except Exception:
        pass


def _get_order_comment_column(cursor):
    """Возвращает имя поля комментария в orders (из существующих или созданного admin_note)."""
    for field_name in ('comment', 'note', 'description', 'admin_note', 'details'):
        cursor.execute(f"SHOW COLUMNS FROM orders LIKE '{field_name}'")
        if cursor.fetchone():
            return field_name
    return 'admin_note'


@login_required_custom
@tech_or_director_required
def crm_orders_list(request):
    """Список заказов. Директор видит все, тех. специалист — свои ручные + из чатов."""
    user = get_current_user(request)
    _ensure_external_catalog_tables()
    _ensure_orders_manual_columns()
    _ensure_address_columns()
    filters = _get_orders_filters_and_scope(request, user)
    is_full_access = filters['is_full_access']

    stats_date_from, stats_date_to = _parse_stats_period("", "")
    initial_stats = _get_orders_stats(
        user,
        stats_date_from,
        stats_date_to,
        source_filter=filters['source_filter'],
        specialist_filter=filters['specialist_filter'],
    )
    total_orders = initial_stats['total']
    with connection.cursor() as cursor:
        cursor.execute(f"""
        SELECT
            o.id, o.user_id, o.company_id, o.amount, o.status, o.source,
            o.payment_id, o.created_at, o.paid_at,
            o.description, o.admin_note AS internal_comment, o.address AS order_address, o.author_name, o.specialist_id,
            u.first_name, u.last_name, u.phone, u.inn, u.email, u.city, u.kpp,
            c.name AS company_name,
            sp.name AS specialist_name, sp.profession AS specialist_profession,
            items.item_names
        FROM orders o
        LEFT JOIN users u ON u.id = o.user_id
        LEFT JOIN companies c ON c.id = o.company_id
        LEFT JOIN specialists sp ON sp.id = o.specialist_id
        LEFT JOIN (
            SELECT
                oi.order_id,
                GROUP_CONCAT(
                    DISTINCT CASE
                        WHEN oi.legacy_item_title IS NOT NULL THEN oi.legacy_item_title
                        WHEN eq.name IS NOT NULL THEN eq.name
                        WHEN cd.id IS NOT NULL THEN
                            CONCAT(
                                COALESCE(eo.name, CONCAT('Внешний товар #', cd.operator_id)),
                                CASE WHEN et.name IS NOT NULL THEN CONCAT(' — ', et.name) ELSE '' END
                            )
                        ELSE CONCAT('Позиция #', oi.id)
                    END
                    ORDER BY oi.id ASC
                    SEPARATOR '\n'
                ) AS item_names
            FROM order_items oi
            LEFT JOIN equipment eq ON eq.id = oi.equipment_id
            LEFT JOIN codes cd ON cd.id = oi.code_id
            LEFT JOIN ext_product_operators eo
              ON eo.category = CASE
                    WHEN cd.code_type IN ('ofd_first', 'ofd_platform', 'ofd_other') THEN 'ofd'
                    WHEN cd.code_type = 'service' THEN 'service'
                    WHEN cd.code_type = 'service_order' THEN 'services_for_orders'
                    WHEN cd.code_type = 'edo' THEN 'edo'
                    ELSE NULL
                 END
             AND eo.operator_id = cd.operator_id
            LEFT JOIN ext_product_types et ON et.type_id = cd.type_id
            GROUP BY oi.order_id
        ) items ON items.order_id = o.id
        {filters['where']}
        ORDER BY o.created_at DESC
        LIMIT 200
    """, filters['params'])
        columns = [col[0] for col in cursor.description]
        orders = [dict(zip(columns, row)) for row in cursor.fetchall()]

    specialists_for_edit = _get_all_specialists() if is_full_access else []

    return render(request, 'bot_app/crm/orders_list.html', {
        'orders': orders,
        'total_orders': total_orders,
        'paid_orders': initial_stats['paid'],
        'advance_orders': initial_stats['advance'],
        'completed_orders': initial_stats['completed'],
        'cancelled_orders': initial_stats['cancelled'],
        'unpaid_orders': initial_stats['unpaid'],
        'revenue': initial_stats['revenue'],
        'advance_sum': initial_stats['advance_sum'],
        'shown_count': len(orders),
        'stats_date_from': stats_date_from.isoformat(),
        'stats_date_to': stats_date_to.isoformat(),
        'stats_date_from_display': stats_date_from.strftime("%d.%m.%Y"),
        'stats_date_to_display': stats_date_to.strftime("%d.%m.%Y"),
        'source_filter': filters['source_filter'],
        'status_filter': filters['status_filter'],
        'specialist_filter': filters['specialist_filter'],
        'q': filters['q'],
        'date_from': filters['date_from'],
        'date_to': filters['date_to'],
        'paid_date_from': filters['paid_date_from'],
        'paid_date_to': filters['paid_date_to'],
        'amount_from': filters['amount_from'],
        'amount_to': filters['amount_to'],
        'active_menu': 'orders',
        'user': user,
        'specialists_for_edit': specialists_for_edit,
        'can_edit_specialist': is_full_access,
    })


@login_required_custom
@tech_or_director_required
def api_orders_stats(request):
    user = get_current_user(request)
    stats_date_from_raw = request.GET.get('stats_date_from', '').strip()
    stats_date_to_raw = request.GET.get('stats_date_to', '').strip()
    source_filter = request.GET.get('source', '').strip()
    specialist_filter = request.GET.get('specialist_id', '').strip()

    stats_date_from, stats_date_to = _parse_stats_period(stats_date_from_raw, stats_date_to_raw)
    stats = _get_orders_stats(
        user,
        stats_date_from,
        stats_date_to,
        source_filter=source_filter,
        specialist_filter=specialist_filter,
    )
    return JsonResponse({
        "success": True,
        "stats": stats,
        "period": {
            "date_from": stats_date_from.isoformat(),
            "date_to": stats_date_to.isoformat(),
            "date_from_display": stats_date_from.strftime("%d.%m.%Y"),
            "date_to_display": stats_date_to.strftime("%d.%m.%Y"),
        },
    })


@login_required_custom
@tech_or_director_required
def export_orders_excel(request):
    from openpyxl import Workbook

    user = get_current_user(request)
    _ensure_external_catalog_tables()
    _ensure_orders_manual_columns()
    _ensure_address_columns()
    filters = _get_orders_filters_and_scope(request, user)
    status_map = {
        'CONFIRMED': 'Оплачен',
        'COMPLETED': 'Выполнена',
        'ADVANCE': 'Аванс',
        'INIT': 'Не оплачен',
        'CANCELLED': 'Отменён',
        'REFUNDED': 'Возврат',
    }
    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT o.id, o.source, o.amount, o.status,
                   COALESCE(items.item_names, o.description, '') AS description,
                   TRIM(CONCAT(COALESCE(u.last_name,''), ' ', COALESCE(u.first_name,''))) AS client_name,
                   c.name AS company_name, u.inn, u.phone, u.email, sp.name AS specialist_name,
                   o.created_at, o.paid_at, o.author_name, o.address
            FROM orders o
            LEFT JOIN users u ON u.id = o.user_id
            LEFT JOIN companies c ON c.id = o.company_id
            LEFT JOIN specialists sp ON sp.id = o.specialist_id
            LEFT JOIN (
                SELECT
                    oi.order_id,
                    GROUP_CONCAT(
                        DISTINCT CASE
                            WHEN oi.legacy_item_title IS NOT NULL THEN oi.legacy_item_title
                            WHEN eq.name IS NOT NULL THEN eq.name
                            WHEN cd.id IS NOT NULL THEN
                                CONCAT(
                                    COALESCE(eo.name, CONCAT('Внешний товар #', cd.operator_id)),
                                    CASE WHEN et.name IS NOT NULL THEN CONCAT(' — ', et.name) ELSE '' END
                                )
                            ELSE CONCAT('Позиция #', oi.id)
                        END
                        ORDER BY oi.id ASC
                        SEPARATOR '\n'
                    ) AS item_names
                FROM order_items oi
                LEFT JOIN equipment eq ON eq.id = oi.equipment_id
                LEFT JOIN codes cd ON cd.id = oi.code_id
                LEFT JOIN ext_product_operators eo
                  ON eo.category = CASE
                        WHEN cd.code_type IN ('ofd_first', 'ofd_platform', 'ofd_other') THEN 'ofd'
                        WHEN cd.code_type = 'service' THEN 'service'
                        WHEN cd.code_type = 'service_order' THEN 'services_for_orders'
                        WHEN cd.code_type = 'edo' THEN 'edo'
                        ELSE NULL
                     END
                 AND eo.operator_id = cd.operator_id
                LEFT JOIN ext_product_types et ON et.type_id = cd.type_id
                GROUP BY oi.order_id
            ) items ON items.order_id = o.id
            {filters['where']}
            ORDER BY o.created_at DESC
        """, filters['params'])
        rows = cursor.fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Сделки"
    ws.append(["ID сделки", "Источник", "Сумма", "Статус", "Описание / товары", "Клиент", "Компания", "ИНН", "Телефон", "Email", "Специалист", "Создана", "Дата оплаты", "Автор", "Адрес"])
    for row in rows:
        row = list(row)
        row[3] = status_map.get(row[3], row[3])
        row[11] = row[11].strftime('%Y-%m-%d %H:%M:%S') if row[11] else ''
        row[12] = row[12].strftime('%Y-%m-%d %H:%M:%S') if row[12] else ''
        ws.append(row)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=orders_export_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.xlsx'
    wb.save(response)
    return response


def _ensure_scp_order_id_column():
    """Добавляет колонку order_id в support_chat_payments если её нет, и заполняет по payment_id."""
    with connection.cursor() as cursor:
        cursor.execute("SHOW COLUMNS FROM support_chat_payments LIKE 'order_id'")
        if not cursor.fetchone():
            try:
                cursor.execute(
                    "ALTER TABLE support_chat_payments ADD COLUMN order_id INT DEFAULT NULL"
                )
                cursor.execute(
                    """UPDATE support_chat_payments scp
                       JOIN orders o ON o.payment_id = scp.payment_id
                                     AND o.payment_id IS NOT NULL AND o.payment_id != ''
                       SET scp.order_id = o.id
                       WHERE scp.order_id IS NULL"""
                )
            except Exception:
                pass


@login_required_custom
@director_required
def crm_order_detail(request, order_id):
    """Карточка заказа (только директор)."""
    user = get_current_user(request)
    _ensure_orders_manual_columns()
    _ensure_address_columns()
    _ensure_scp_order_id_column()

    back_url = (request.GET.get('next') or '').strip()
    with connection.cursor() as cursor:
        # Основные данные заказа
        cursor.execute("""
            SELECT
                o.id, o.user_id, o.company_id, o.specialist_id,
                o.amount, o.status, o.source, o.payment_id,
                o.created_at, o.paid_at, o.updated_at,
                o.description, o.admin_note AS internal_comment, o.address AS order_address, o.author_name,
                o.payment_info,
                u.first_name, u.last_name, u.phone, u.inn, u.email,
                u.org_name, u.kpp, u.city, u.tg_username, u.tg_user_id, u.max_user_id,
                c.name AS company_name,
                sp.name AS specialist_name, sp.profession AS specialist_profession
            FROM orders o
            LEFT JOIN users u ON u.id = o.user_id
            LEFT JOIN companies c ON c.id = o.company_id
            LEFT JOIN specialists sp ON sp.id = o.specialist_id
            WHERE o.id = %s
        """, [order_id])
        cols = [col[0] for col in cursor.description]
        row = cursor.fetchone()
        if not row:
            return redirect('bot_app:crm_orders')
        order = dict(zip(cols, row))

        # Позиции заказа
        cursor.execute("""
            SELECT
                oi.id, oi.qty, oi.price,
                oi.legacy_item_title, oi.legacy_item_type,
                eq.name AS equipment_name, eq.id AS equipment_id,
                COALESCE(eo.name, CONCAT('Внешний товар #', cd.operator_id)) AS ext_operator_name,
                et.name AS ext_type_name,
                cd.code_type, cd.code_value, cd.id AS code_id
            FROM order_items oi
            LEFT JOIN equipment eq ON eq.id = oi.equipment_id
            LEFT JOIN codes cd ON cd.id = oi.code_id
            LEFT JOIN ext_product_operators eo
              ON eo.category = CASE
                    WHEN cd.code_type IN ('ofd_first', 'ofd_platform', 'ofd_other') THEN 'ofd'
                    WHEN cd.code_type = 'service' THEN 'service'
                    WHEN cd.code_type = 'service_order' THEN 'services_for_orders'
                    WHEN cd.code_type = 'edo' THEN 'edo'
                    ELSE NULL
                 END
              AND eo.operator_id = cd.operator_id
            LEFT JOIN ext_product_types et ON et.type_id = cd.type_id
            WHERE oi.order_id = %s
            ORDER BY oi.id
        """, [order_id])
        cols_items = [col[0] for col in cursor.description]
        items = [dict(zip(cols_items, r)) for r in cursor.fetchall()]

        # Связанный платёж в чате (сделка)
        chat_payment = None
        chat_info = None
        payment_id = order.get('payment_id')
        if payment_id:
            cursor.execute("""
                SELECT scp.id, scp.chat_id, scp.amount_rub, scp.status,
                       scp.created_at, scp.pay_url,
                       sc.category, sc.status AS chat_status,
                       sp2.name AS chat_specialist_name
                FROM support_chat_payments scp
                LEFT JOIN support_chats sc ON sc.id = scp.chat_id
                LEFT JOIN specialists sp2 ON sp2.id = scp.specialist_id
                WHERE scp.payment_id = %s
                LIMIT 1
            """, [payment_id])
            row_scp = cursor.fetchone()
            if row_scp:
                cols_scp = [col[0] for col in cursor.description]
                chat_payment = dict(zip(cols_scp, row_scp))

    return render(request, 'bot_app/crm/order_detail.html', {
        'order': order,
        'items': items,
        'chat_payment': chat_payment,
        'user': user,
        'active_menu': 'crm_orders',
        'back_url': back_url,
    })


@login_required_custom
@tech_or_director_required
@require_POST
def api_order_manual_create(request):
    """Создать ручную сделку (заказ с source='manual'). Доступно директору и тех. специалистам."""
    user = get_current_user(request)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        return JsonResponse({'success': False, 'error': str(e)})

    result = create_manual_deal(user, data)
    return JsonResponse(result)


@login_required_custom
@tech_or_director_required
@require_POST
def api_order_update(request, order_id):
    """Обновить сделку: сумма, статус, описание, комментарий, дата оплаты и (для директора) специалист."""
    user = get_current_user(request)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        return JsonResponse({'success': False, 'error': str(e)})

    result = update_order(user, order_id, data)
    return JsonResponse(result)


@login_required_custom
@tech_or_director_required
@require_POST
def api_order_delete(request, order_id):
    """Удалить заказ и его позиции. Тех. специалист может удалить только свои сделки."""
    user = get_current_user(request)
    result = delete_order(user, order_id)
    return JsonResponse(result)


@login_required_custom
@tech_or_director_required
@require_POST
def api_order_create_client(request):
    """Создать клиента прямо из модалки создания сделки. Доступно директору и тех. специалистам."""
    user = get_current_user(request)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        return JsonResponse({'success': False, 'error': str(e)})

    result = create_order_client(user, data)
    return JsonResponse(result)


@login_required_custom
@tech_or_director_required
def api_orders_search_clients(request):
    """Поиск клиентов и компаний для выбора при создании ручной сделки. Доступно директору и тех. специалистам."""
    q = request.GET.get('q', '').strip()
    result = search_order_clients_and_companies(q)
    return JsonResponse(result)
