"""CRM task views extracted from the legacy views module."""

import json
from io import BytesIO
from datetime import date, timedelta

from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from ..permissions import (
    director_required,
    get_current_user,
    login_required_custom,
)
from ..sql_cursor import with_sql_rewrite
from ..task_workflow import TaskWorkflowError, normalize_status
from ..services.task_service import (
    TASK_CLIENT_ACCESS_STATUSES,
    _add_task_history,
    _build_client_brief,
    _ensure_task_history_table,
    _ensure_task_clients_table,
    _ensure_tasks_table,
    _ensure_tasks_schema,
    _extract_linked_client_ids_from_meta,
    _fetch_clients_brief,
    _get_default_director_id,
    _get_task_client_ids,
    _normalize_linked_client_ids,
    _resolve_task_linked_client_ids,
    _set_task_clients,
    _specialist_has_task_client_access,
    _task_requires_card_verification_before_complete,
    _upsert_task,
    create_task_from_payload,
    delete_task,
    update_task_from_payload,
)
from .dashboard_helpers import _get_all_clients

connection = with_sql_rewrite(connection)


TASK_PRIORITY_LABELS = {
    'low': 'Низкий',
    'medium': 'Средний',
    'high': 'Высокий',
}

TASK_STATUS_LABELS = {
    'awaiting_assignment': 'Ожидает назначения',
    'in_progress': 'В работе',
    'postponed': 'Отложена',
    'done': 'Выполнена',
}

TASK_SOURCE_LABELS = {
    'deal': 'Сделка',
    'renewal': 'Продление',
    'order': 'Заказ',
    'manual': 'Вручную',
}

TASK_EXPORT_COLUMNS = [
    'ID',
    'Приоритет',
    'Статус',
    'Заголовок',
    'Описание',
    'Источник',
    'ID источника',
    'Исполнитель',
    'Создал',
    'Дедлайн',
    'Создано',
    'Обновлено',
    'Выполнено',
    'Клиенты/мета',
]


def _sync_kkt_expiry_notifications():
    """Lazy-load notification sync to avoid module-level view import cycles."""
    from .notifications import _sync_kkt_expiry_notifications as sync_kkt_expiry_notifications

    return sync_kkt_expiry_notifications()

def _sync_deal_and_order_tasks_from_orders():
    """Синхронизирует задачи из orders:
    - deal: только ручные сделки (source='manual')
    - order: все оплаты из ботов + оплаты в чатах специалистов
    """
    _ensure_tasks_table()
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                o.id, o.user_id, o.specialist_id, o.amount, o.status, o.source,
                o.description, o.author_name, o.created_at,
                u.first_name, u.last_name, u.org_name, u.inn, u.phone, u.email, u.city, u.kpp,
                sp.name AS specialist_name
            FROM orders o
            LEFT JOIN users u ON u.id = o.user_id
            LEFT JOIN specialists sp ON sp.id = o.specialist_id
            WHERE o.created_at >= NOW() - INTERVAL 180 DAY
            ORDER BY o.id DESC
            LIMIT 2000
        """)
        cols = [c[0] for c in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

    for row in rows:
        client_name = row.get('org_name') or f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or f"Клиент #{row['user_id']}"
        source = (row.get('source') or '').lower()
        is_chat_payment = bool(row.get('specialist_id')) or ((row.get('description') or '').lower().startswith('оплата в чате'))
        is_manual_deal = source == 'manual'
        is_bot_or_chat_order = source in ('telegram', 'max')
        should_create_order_task = is_bot_or_chat_order or is_chat_payment

        if is_manual_deal:
            title = f"Сделка #{row['id']} — {client_name}"
            descr = (row.get('description') or '').strip() or f"Сделка #{row['id']}"
            _upsert_task(
                title=title,
                description=descr,
                source_type='deal',
                source_id=row['id'],
                dedupe_key=f"deal:order:{row['id']}",
                assignee_id=None,
                priority='high',
                meta={
                    'client_id': row.get('user_id'),
                    'client_name': client_name,
                    'inn': row.get('inn'),
                    'phone': row.get('phone'),
                    'email': row.get('email'),
                    'city': row.get('city'),
                    'kpp': row.get('kpp'),
                    'channel': row.get('source'),
                    'specialist': row.get('specialist_name') or row.get('author_name'),
                    'amount': row.get('amount'),
                    'payment_status': row.get('status'),
                },
                ensure_schema=False,
            )
        elif should_create_order_task:
            title = f"Заказ #{row['id']} — {client_name}"
            descr = (row.get('description') or '').strip() or f"Заказ #{row['id']}"
            _upsert_task(
                title=title,
                description=descr,
                source_type='order',
                source_id=row['id'],
                dedupe_key=f"order:order:{row['id']}",
                priority='high' if is_chat_payment else 'medium',
                meta={
                    'order_id': row['id'],
                    'client_id': row.get('user_id'),
                    'client_name': client_name,
                    'channel': source,
                    'payment_origin': 'specialist_chat' if is_chat_payment else 'bot_checkout',
                    'amount': row.get('amount'),
                    'status': row.get('status'),
                },
                ensure_schema=False,
            )



def _parse_task_filter_date(raw_value):
    """Return a YYYY-MM-DD date object for valid task filter input, else None."""
    if not raw_value:
        return None
    try:
        return date.fromisoformat(str(raw_value).strip())
    except (TypeError, ValueError):
        return None


def _build_task_filters(request, user, *, include_status=True):
    """Build reusable SQL WHERE clauses and normalized filters for the CRM tasks list."""
    is_director = bool(user.get('is_director'))

    status_filter = (request.GET.get('status', '') or '').strip()
    assignee_filter = (request.GET.get('assignee', '') or '').strip()
    source_filter = (request.GET.get('source', '') or '').strip()
    priority_filter = (request.GET.get('priority', '') or '').strip()
    client_filter = (request.GET.get('client_id', '') or '').strip()
    search_query = (request.GET.get('q', '') or '').strip()
    raw_date_from = (request.GET.get('date_from', '') or '').strip()
    raw_date_to = (request.GET.get('date_to', '') or '').strip()

    base_conditions = []
    base_params = []
    status_condition = None
    status_params = []

    # Специалист видит только свои задачи.
    if not is_director:
        base_conditions.append("t.assignee_id = %s")
        base_params.append(user['id'])
        # Для специалиста дефолтный экран задач — «В работе».
        if not status_filter:
            status_filter = 'in_progress'

    if status_filter:
        if status_filter == 'overdue':
            status_condition = "t.deadline < NOW() AND t.status NOT IN ('done')"
        elif status_filter == 'awaiting':
            status_condition = "t.status = 'awaiting_assignment'"
        elif status_filter == 'in_progress':
            status_condition = "t.status = 'in_progress'"
        elif status_filter == 'postponed':
            status_condition = "t.status = 'postponed'"
        elif status_filter in ('done', 'awaiting_assignment', 'in_progress', 'postponed'):
            status_condition = "t.status = %s"
            status_params.append(status_filter)
        else:
            status_filter = ''

    if assignee_filter and is_director:
        try:
            assignee_id = int(assignee_filter)
            base_conditions.append("t.assignee_id = %s")
            base_params.append(assignee_id)
            assignee_filter = str(assignee_id)
        except (TypeError, ValueError):
            assignee_filter = ''
    elif not is_director:
        assignee_filter = ''

    if source_filter:
        base_conditions.append("t.source_type = %s")
        base_params.append(source_filter)
    if priority_filter:
        base_conditions.append("t.priority = %s")
        base_params.append(priority_filter)
    if client_filter:
        try:
            client_filter_id = int(client_filter)
            base_conditions.append(
                "EXISTS (SELECT 1 FROM task_clients tc_filter "
                "WHERE tc_filter.task_id = t.id AND tc_filter.client_id = %s)"
            )
            base_params.append(client_filter_id)
            client_filter = str(client_filter_id)
        except (TypeError, ValueError):
            client_filter = ''

    if search_query:
        search_parts = [
            "t.title LIKE %s",
            "t.description LIKE %s",
            "cr.name LIKE %s",
            "asg.name LIKE %s",
            "t.meta_json LIKE %s",
        ]
        like_query = f"%{search_query}%"
        search_params = [like_query, like_query, like_query, like_query, like_query]
        if search_query.isdigit():
            numeric_query = int(search_query)
            search_parts.insert(0, "t.id = %s")
            search_params.insert(0, numeric_query)
            search_parts.insert(3, "t.source_id = %s")
            search_params.insert(3, numeric_query)
        base_conditions.append("(" + " OR ".join(search_parts) + ")")
        base_params.extend(search_params)

    date_from_obj = _parse_task_filter_date(raw_date_from)
    if date_from_obj:
        base_conditions.append("t.created_at >= %s")
        base_params.append(f"{date_from_obj.isoformat()} 00:00:00")
        raw_date_from = date_from_obj.isoformat()
    else:
        raw_date_from = ''

    date_to_obj = _parse_task_filter_date(raw_date_to)
    if date_to_obj:
        base_conditions.append("t.created_at < %s")
        base_params.append(f"{(date_to_obj + timedelta(days=1)).isoformat()} 00:00:00")
        raw_date_to = date_to_obj.isoformat()
    else:
        raw_date_to = ''

    where_parts = list(base_conditions)
    where_params = list(base_params)
    if include_status and status_condition:
        where_parts.append(status_condition)
        where_params.extend(status_params)

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    base_where_sql = ("WHERE " + " AND ".join(base_conditions)) if base_conditions else ""
    normalized_filters = {
        'status': status_filter,
        'assignee': assignee_filter,
        'source': source_filter,
        'priority': priority_filter,
        'client_id': client_filter,
        'q': search_query,
        'date_from': raw_date_from,
        'date_to': raw_date_to,
    }

    return where_sql, where_params, base_where_sql, base_params, normalized_filters



def _format_date_ru(value):
    """Return DD.MM.YYYY for a date or ISO date string."""
    if not value:
        return ''
    if isinstance(value, date):
        return value.strftime('%d.%m.%Y')
    parsed = _parse_task_filter_date(value)
    return parsed.strftime('%d.%m.%Y') if parsed else str(value)


def _get_default_task_stats_period(today=None):
    """Return default task stats period as YYYY-MM-DD strings for the current month."""
    today = today or timezone.localdate()
    first_day = today.replace(day=1)
    if first_day.month == 12:
        next_month = first_day.replace(year=first_day.year + 1, month=1)
    else:
        next_month = first_day.replace(month=first_day.month + 1)
    last_day = next_month - timedelta(days=1)
    return first_day.isoformat(), last_day.isoformat()


def _get_task_stats_period_from_request(request):
    """Return validated task stats period dates and ISO strings from request GET."""
    default_from, default_to = _get_default_task_stats_period()
    raw_date_from = (request.GET.get('stats_date_from', '') or '').strip()
    raw_date_to = (request.GET.get('stats_date_to', '') or '').strip()
    date_from_obj = _parse_task_filter_date(raw_date_from)
    date_to_obj = _parse_task_filter_date(raw_date_to)
    if not date_from_obj or not date_to_obj or date_from_obj > date_to_obj:
        date_from_obj = date.fromisoformat(default_from)
        date_to_obj = date.fromisoformat(default_to)
    return date_from_obj, date_to_obj, date_from_obj.isoformat(), date_to_obj.isoformat()


def _build_task_stats(request, user):
    """Calculate task stats using stats_date_* period and non-status task filters."""
    date_from_obj, date_to_obj, stats_date_from, stats_date_to = _get_task_stats_period_from_request(request)

    stats_get = request.GET.copy()
    stats_get.pop('date_from', None)
    stats_get.pop('date_to', None)
    stats_get.pop('status', None)

    class StatsRequest:
        GET = stats_get

    _, _, stat_where, base_params, _ = _build_task_filters(StatsRequest(), user, include_status=False)

    period_condition = "t.created_at >= %s AND t.created_at < %s"
    period_params = [
        f"{stats_date_from} 00:00:00",
        f"{(date_to_obj + timedelta(days=1)).isoformat()} 00:00:00",
    ]
    if stat_where:
        stat_where = f"{stat_where} AND {period_condition}"
        stat_params = [*base_params, *period_params]
    else:
        stat_where = f"WHERE {period_condition}"
        stat_params = period_params

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT
                COUNT(*)                                                      AS total,
                SUM(t.status = 'awaiting_assignment')                         AS cnt_awaiting,
                SUM(t.status = 'in_progress')                                 AS cnt_in_progress,
                SUM(t.status = 'postponed')                                   AS cnt_postponed,
                SUM(t.status = 'done')                                        AS cnt_done,
                SUM(t.deadline < NOW() AND t.status NOT IN ('done'))          AS cnt_overdue
            FROM tasks t
            LEFT JOIN specialists cr  ON cr.id  = t.created_by
            LEFT JOIN specialists asg ON asg.id = t.assignee_id
            {stat_where}
        """, stat_params)
        row = cursor.fetchone() or (0, 0, 0, 0, 0, 0)

    stats = {
        'total': int(row[0] or 0),
        'awaiting': int(row[1] or 0),
        'in_progress': int(row[2] or 0),
        'postponed': int(row[3] or 0),
        'done': int(row[4] or 0),
        'overdue': int(row[5] or 0),
    }
    period = {
        'date_from': stats_date_from,
        'date_to': stats_date_to,
        'date_from_display': _format_date_ru(stats_date_from),
        'date_to_display': _format_date_ru(stats_date_to),
    }
    return stats, period

def _format_task_export_datetime(value):
    """Format DB datetime/date values for task export."""
    if not value:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime('%d.%m.%Y %H:%M')
    return str(value)


def _format_task_export_value(value):
    """Return an empty string for NULL-like Excel values."""
    if value is None:
        return ''
    return value


def _stringify_task_export_meta_value(value):
    """Compact nested meta values for the human-readable export summary."""
    if value in (None, ''):
        return ''
    if isinstance(value, dict):
        name = value.get('name') or value.get('client_name') or value.get('title')
        client_id = value.get('id') or value.get('client_id')
        if name and client_id:
            return f'{name} (ID {client_id})'
        if name:
            return str(name)
        if client_id:
            return f'ID {client_id}'
        return ', '.join(
            f'{key}: {_stringify_task_export_meta_value(item)}'
            for key, item in value.items()
            if item not in (None, '', [], {})
        )
    if isinstance(value, (list, tuple)):
        return '; '.join(
            item_text
            for item_text in (_stringify_task_export_meta_value(item) for item in value)
            if item_text
        )
    return str(value)


def _task_export_meta_summary(meta_json):
    """Build a short human-readable meta summary for the Excel export."""
    if not meta_json:
        return ''

    if isinstance(meta_json, dict):
        meta = meta_json
    else:
        raw_meta = str(meta_json).strip()
        if not raw_meta:
            return ''
        try:
            meta = json.loads(raw_meta)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw_meta[:300]

    if not isinstance(meta, dict):
        return _stringify_task_export_meta_value(meta)[:300]

    field_labels = [
        ('client_name', 'Клиент'),
        ('inn', 'ИНН'),
        ('phone', 'Телефон'),
        ('amount', 'Сумма'),
        ('channel', 'Канал'),
        ('linked_clients', 'Связанные клиенты'),
    ]
    summary_parts = []
    for field, label in field_labels:
        value_text = _stringify_task_export_meta_value(meta.get(field))
        if value_text:
            summary_parts.append(f'{label}: {value_text}')

    if not summary_parts:
        linked_client_ids = _stringify_task_export_meta_value(meta.get('linked_client_ids'))
        if linked_client_ids:
            summary_parts.append(f'Связанные клиенты: {linked_client_ids}')

    return '; '.join(summary_parts)[:300]


@login_required_custom
def export_tasks_excel(request):
    """Export currently filtered CRM tasks to an Excel workbook."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    user = get_current_user(request)
    if not user:
        return redirect('bot_app:login')

    _ensure_tasks_table()
    _sync_deal_and_order_tasks_from_orders()
    _sync_kkt_expiry_notifications()

    where, where_params, _, _, filters = _build_task_filters(request, user)

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT t.id, t.title, t.description, t.source_type, t.source_id,
                   t.priority, t.status, t.deadline, t.created_at, t.updated_at,
                   t.completed_at,
                   cr.name AS creator_name,
                   asg.name AS assignee_name,
                   t.meta_json
            FROM tasks t
            LEFT JOIN specialists cr  ON cr.id  = t.created_by
            LEFT JOIN specialists asg ON asg.id = t.assignee_id
            {where}
            ORDER BY
                CASE WHEN t.status IN ('awaiting_assignment', 'in_progress') THEN 0 ELSE 1 END ASC,
                t.created_at DESC
        """, where_params)
        columns = [col[0] for col in cursor.description]
        tasks = [dict(zip(columns, row)) for row in cursor.fetchall()]

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = 'Задачи CRM'

    worksheet.append(TASK_EXPORT_COLUMNS)
    header_fill = PatternFill(fill_type='solid', fgColor='1F4E78')
    header_font = Font(color='FFFFFF', bold=True)
    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment

    for task in tasks:
        raw_status = (task.get('status') or '').strip().lower()
        try:
            normalized_status = normalize_status(raw_status)
        except TaskWorkflowError:
            normalized_status = raw_status

        worksheet.append([
            _format_task_export_value(task.get('id')),
            TASK_PRIORITY_LABELS.get((task.get('priority') or '').strip().lower(), task.get('priority') or ''),
            TASK_STATUS_LABELS.get(normalized_status, normalized_status),
            _format_task_export_value(task.get('title')),
            _format_task_export_value(task.get('description')),
            TASK_SOURCE_LABELS.get((task.get('source_type') or '').strip().lower(), task.get('source_type') or ''),
            _format_task_export_value(task.get('source_id')),
            _format_task_export_value(task.get('assignee_name')),
            _format_task_export_value(task.get('creator_name')),
            _format_task_export_datetime(task.get('deadline')),
            _format_task_export_datetime(task.get('created_at')),
            _format_task_export_datetime(task.get('updated_at')),
            _format_task_export_datetime(task.get('completed_at')),
            _task_export_meta_summary(task.get('meta_json')),
        ])

    worksheet.freeze_panes = 'A2'
    worksheet.auto_filter.ref = worksheet.dimensions
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical='top', wrap_text=True)

    for column_cells in worksheet.columns:
        column_letter = get_column_letter(column_cells[0].column)
        max_length = max(
            len(str(cell.value))
            for cell in column_cells
            if cell.value is not None
        ) if column_cells else 0
        worksheet.column_dimensions[column_letter].width = min(max(max_length + 2, 10), 60)

    worksheet.sheet_properties.pageSetUpPr.fitToPage = True

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    filename = f"crm_tasks_{timezone.localtime().strftime('%Y%m%d_%H%M')}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def crm_tasks_list(request):
    """Список задач. Директор видит все, специалист — только свои."""
    user = get_current_user(request)
    if not user:
        return redirect('bot_app:login')

    _ensure_tasks_table()
    _sync_deal_and_order_tasks_from_orders()
    _sync_kkt_expiry_notifications()

    is_director = user.get('is_director')

    where, where_params, _, _, filters = _build_task_filters(request, user)
    status_filter = filters['status']
    assignee_filter = filters['assignee']
    source_filter = filters['source']
    priority_filter = filters['priority']
    client_filter = filters['client_id']
    search_query = filters['q']
    date_from = filters['date_from']
    date_to = filters['date_to']
    prefill_client_ids_raw = request.GET.get('prefill_client_ids', '')
    prefill_single_client_id = request.GET.get('prefill_client_id', '')

    stats, stats_period = _build_task_stats(request, user)

    all_clients = []
    with connection.cursor() as cursor:
        # Задачи
        # is_new_unread: пометка «Новая» — задача ещё не назначена (awaiting_assignment без assignee_id)
        # для директора, или назначена но специалист ещё не начал работу — для специалиста.
        cursor.execute(f"""
            SELECT t.id, t.title, t.description, t.source_type, t.source_id, t.meta_json,
                   t.created_by, t.director_id, t.assignee_id, t.assigned_at, t.deadline, t.priority,
                   t.status, t.result_comment, t.postponed_until, t.completed_at, t.created_at, t.updated_at,
                   t.client_id,
                   cr.name AS creator_name,
                   asg.name AS assignee_name,
                   CASE WHEN t.deadline < NOW() AND t.status NOT IN ('done')
                        THEN 1 ELSE 0
                   END AS is_overdue,
                   CASE WHEN t.status = 'awaiting_assignment' AND t.assignee_id IS NULL
                        THEN 1 ELSE 0
                   END AS is_new_unread
            FROM tasks t
            LEFT JOIN specialists cr  ON cr.id  = t.created_by
            LEFT JOIN specialists asg ON asg.id = t.assignee_id
            {where}
            ORDER BY
                CASE WHEN t.status IN ('awaiting_assignment', 'in_progress') THEN 0 ELSE 1 END ASC,
                t.created_at DESC
            LIMIT 300
        """, where_params)
        columns = [col[0] for col in cursor.description]
        tasks = [dict(zip(columns, row)) for row in cursor.fetchall()]
        for task in tasks:
            try:
                task['status'] = normalize_status(task.get('status'))
            except TaskWorkflowError:
                # Фоллбек для архивных/нестандартных статусов — оставляем как есть.
                task['status'] = (task.get('status') or '').strip().lower()
            raw_meta = task.get('meta_json')
            if raw_meta:
                try:
                    task['meta'] = json.loads(raw_meta)
                except Exception:
                    task['meta'] = {}
            else:
                task['meta'] = {}
            linked_client_ids = _resolve_task_linked_client_ids(
                task_id=task['id'],
                task_row=task,
                meta=task['meta'],
            )
            if linked_client_ids:
                task['meta']['linked_client_ids'] = linked_client_ids
                task['meta']['linked_clients'] = _fetch_clients_brief(linked_client_ids)
            else:
                task['meta'].pop('linked_client_ids', None)
                task['meta'].pop('linked_clients', None)
            task['requires_card_verification_before_complete'] = _task_requires_card_verification_before_complete(
                user=user,
                task_row=task,
                linked_client_ids=linked_client_ids,
            )
            # Для специалистов показываем чаты только по клиентам,
            # явно прикреплённым к задаче (task_clients).
            # Фоллбек к legacy-полям оставляем только директору.
            chat_client_ids = list(linked_client_ids)
            task['meta'].pop('linked_chat', None)
            task['meta'].pop('linked_chats', None)
            task['meta'].pop('linked_chats_archive', None)
            if chat_client_ids:
                with connection.cursor() as chat_cursor:
                    placeholders = ", ".join(["%s"] * len(chat_client_ids))
                    chat_cursor.execute(f"""
                        SELECT id, client_user_id, status, updated_at
                        FROM support_chats
                        WHERE client_user_id IN ({placeholders})
                        ORDER BY updated_at DESC, id DESC
                        LIMIT 30
                    """, chat_client_ids)
                    chat_rows = chat_cursor.fetchall()
                    active_statuses = {'pending', 'active', 'consultation'}
                    active_chats = []
                    archive_chats = []
                    for chat_row in chat_rows:
                        chat_item = {
                            'id': int(chat_row[0]),
                            'client_id': int(chat_row[1]),
                            'status': chat_row[2],
                            'updated_at': chat_row[3].strftime('%d.%m.%Y %H:%M') if chat_row[3] else '',
                        }
                        if chat_item['status'] in active_statuses:
                            active_chats.append(chat_item)
                        else:
                            archive_chats.append(chat_item)
                    if active_chats:
                        task['meta']['linked_chats'] = active_chats
                        task['meta']['linked_chat'] = active_chats[0]
                    if archive_chats:
                        task['meta']['linked_chats_archive'] = archive_chats
                        if not task['meta'].get('linked_chat'):
                            task['meta']['linked_chat'] = archive_chats[0]
            # Безопасная сериализация для JS в шаблоне задач (чтобы не ломать клики по строкам).
            task['meta_js'] = json.dumps(task['meta'], ensure_ascii=False)

        # Список специалистов (для формы создания задачи)
        specialists = []
        if is_director:
            cursor.execute("""
                SELECT id, name, profession FROM specialists
                WHERE is_active = 1
                ORDER BY name
            """)
            specialists = [
                {'id': r[0], 'name': r[1], 'profession': r[2]}
                for r in cursor.fetchall()
            ]
            all_clients = _get_all_clients(limit=400)

    prefill_client_ids = []
    if prefill_client_ids_raw:
        for raw_client_id in str(prefill_client_ids_raw).split(','):
            try:
                client_id = int(raw_client_id.strip())
            except (TypeError, ValueError):
                continue
            if client_id > 0 and client_id not in prefill_client_ids:
                prefill_client_ids.append(client_id)
    if not prefill_client_ids and prefill_single_client_id:
        try:
            single_client_id = int(prefill_single_client_id)
            if single_client_id > 0:
                prefill_client_ids.append(single_client_id)
        except (TypeError, ValueError):
            pass

    prefill_clients = _fetch_clients_brief(prefill_client_ids) if prefill_client_ids else []

    return render(request, 'bot_app/crm/tasks_list.html', {
        'tasks': tasks,
        'stats': stats,
        'specialists': specialists,
        'status_filter': status_filter,
        'assignee_filter': assignee_filter,
        'source_filter': source_filter,
        'priority_filter': priority_filter,
        'client_filter': client_filter,
        'search_query': search_query,
        'date_from': date_from,
        'date_to': date_to,
        'stats_date_from': stats_period['date_from'],
        'stats_date_to': stats_period['date_to'],
        'stats_date_from_display': stats_period['date_from_display'],
        'stats_date_to_display': stats_period['date_to_display'],
        'filters': filters,
        'normalized_filters': filters,
        'active_menu': 'tasks',
        'user': user,
        'all_clients': all_clients,
        'prefill_clients': prefill_clients,
    })


@login_required_custom
def api_tasks_stats(request):
    """Return task stats for a selected creation-date period without page reload."""
    user = get_current_user(request)
    if not user:
        return JsonResponse({'success': False, 'error': 'Требуется авторизация'}, status=401)

    _ensure_tasks_table()
    stats, period = _build_task_stats(request, user)

    return JsonResponse({
        'success': True,
        'period': period,
        'stats': stats,
    })


def _task_service_json_response(service_result):
    payload, status = service_result
    if status is None:
        return JsonResponse(payload)
    return JsonResponse(payload, status=status)


def api_task_create(request):
    """Создать задачу (только директор)."""
    user = get_current_user(request)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    return _task_service_json_response(create_task_from_payload(user, data))


def api_task_update(request, task_id):
    """Обновить задачу. Директор — все поля, специалист — только статус и комментарий."""
    user = get_current_user(request)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    return _task_service_json_response(update_task_from_payload(user, task_id, data))


def api_task_delete(request, task_id):
    """Удалить задачу (только директор)."""
    user = get_current_user(request)
    return _task_service_json_response(delete_task(user, task_id))
