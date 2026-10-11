"""Specialist panel view."""

from django.db import connection
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie

from ..permissions import get_current_user, login_required_custom
from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)


def _ensure_support_chats_time_columns_lazy(cursor):
    """Lazy-load chat schema helper from the extracted chat views module."""
    from .chats import _ensure_support_chats_time_columns

    return _ensure_support_chats_time_columns(cursor)


def _ensure_tasks_table_lazy():
    """Lazy-load task schema helper from the extracted task views module."""
    from .tasks import _ensure_tasks_table

    return _ensure_tasks_table()


def _ensure_task_clients_table_lazy(cursor):
    """Lazy-load task-client schema helper from the extracted task views module."""
    from .tasks import _ensure_task_clients_table

    return _ensure_task_clients_table(cursor)


@ensure_csrf_cookie
@login_required_custom
def specialist_panel(request):
    """Панель специалиста — экран чатов + краткая сводка по задачам."""
    user = get_current_user(request)
    if user.get('is_partner'):
        return redirect('bot_app:partner_clients')
    is_director = user.get('is_director')

    # SLA для просрочки (секунды)
    OVERDUE_SLA_SECONDS = 600  # 10 минут

    with connection.cursor() as cursor:
        _ensure_support_chats_time_columns_lazy(cursor)

        # Чаты специалиста с метриками:
        # - назначенные на него;
        # - unassigned pending его профессии (для принятия на сайте).
        cursor.execute("""
            SELECT
                sc.id, sc.client_user_id, sc.status, sc.profession, sc.category,
                sc.source, sc.request_type,
                sc.created_at, sc.accepted_at, sc.closed_at,
                (SELECT COUNT(*) FROM support_chat_messages WHERE chat_id = sc.id) as message_count,
                (SELECT text FROM support_chat_messages WHERE chat_id = sc.id ORDER BY created_at DESC LIMIT 1) as last_message,
                u.first_name, u.last_name, u.phone, u.tg_username,
                TIMESTAMPDIFF(SECOND, sc.created_at, COALESCE(sc.accepted_at, NOW())) AS wait_seconds
            FROM support_chats sc
            LEFT JOIN users u ON u.id = sc.client_user_id
            WHERE (
                    sc.specialist_id = %s
                    OR (
                        sc.status = 'pending'
                        AND sc.specialist_id IS NULL
                        AND sc.profession = %s
                    )
                  )
              AND (sc.request_type IS NULL OR sc.request_type != 'order')
            ORDER BY sc.created_at DESC
        """, [user['id'], int(user.get('profession') or 0)])
        columns = ['id', 'client_user_id', 'status', 'profession', 'category',
                   'source', 'request_type', 'created_at', 'accepted_at', 'closed_at',
                   'message_count', 'last_message',
                   'client_first_name', 'client_last_name', 'client_phone', 'client_tg',
                   'wait_seconds']
        all_chats = [dict(zip(columns, row)) for row in cursor.fetchall()]

    # Категоризация
    completed_chats = []  # Выполненные задачи (закрытые)
    in_progress_chats = []  # В работе (active / consultation / pending с назначением)
    overdue_chats = []  # Просроченные (SLA > 10 мин)
    pending_chats = []  # Ожидают принятия (требуют реакции)

    for chat in all_chats:
        wait = chat.get('wait_seconds') or 0
        is_overdue = wait > OVERDUE_SLA_SECONDS

        if chat['status'] == 'closed':
            completed_chats.append(chat)
            if is_overdue:
                overdue_chats.append(chat)
        elif chat['status'] in ('active', 'consultation'):
            in_progress_chats.append(chat)
            if is_overdue:
                overdue_chats.append(chat)
        elif chat['status'] == 'pending':
            # Pending с назначенным специалистом — тоже в работе
            in_progress_chats.append(chat)
            pending_chats.append(chat)
            if is_overdue:
                overdue_chats.append(chat)

    # --- Сводка по задачам специалиста (без списка, список живёт только в /crm/tasks/) ---
    _ensure_tasks_table_lazy()
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(status = 'done') AS cnt_done,
                SUM(status = 'in_progress') AS cnt_in_progress,
                SUM(status = 'awaiting_assignment') AS cnt_awaiting
            FROM tasks WHERE assignee_id = %s
        """, [user['id']])
        tr = cursor.fetchone()
        task_summary = {
            'total': tr[0] or 0,
            'done': tr[1] or 0,
            'in_progress': tr[2] or 0,
            'awaiting': tr[3] or 0,
        }
        _ensure_task_clients_table_lazy(cursor)
        cursor.execute("""
            SELECT COUNT(DISTINCT tc.client_id)
            FROM task_clients tc
            INNER JOIN tasks t ON t.id = tc.task_id
            WHERE t.assignee_id = %s
              AND t.status = 'in_progress'
        """, [user['id']])
        active_task_clients_count = (cursor.fetchone() or [0])[0] or 0

    return render(request, 'bot_app/specialist_panel.html', {
        'user': user,
        'is_director': is_director,
        'all_chats': all_chats,
        'completed_chats': completed_chats,
        'in_progress_chats': in_progress_chats,
        'overdue_chats': overdue_chats,
        'pending_chats': pending_chats,
        # Счётчики панели специалиста = только чаты (без смешивания с задачами директора).
        'completed_count': len(completed_chats),
        'in_progress_count': len(in_progress_chats),
        'overdue_count': len(overdue_chats),
        'pending_count': len(pending_chats),
        'task_summary': task_summary,
        'active_task_clients_count': active_task_clients_count,
    })
