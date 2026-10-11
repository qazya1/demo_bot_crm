"""Calendar event API views."""

import json
from datetime import date

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from ..permissions import get_current_user, login_required_custom
from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)


@login_required_custom
def api_calendar_events(request):
    """API: получить события за месяц (GET ?year=&month=)"""
    year = int(request.GET.get('year', date.today().year))
    month = int(request.GET.get('month', date.today().month))

    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                    e.id, e.event_date, e.event_time, e.title, e.description,
                    e.category_id, COALESCE(c.name, '') AS category_name,
                    COALESCE(c.color, '#667eea') AS category_color,
                    e.client_id,
                    COALESCE(u.first_name, '') AS client_first_name,
                    COALESCE(u.last_name, '') AS client_last_name,
                    e.created_at
                FROM calendar_events e
                LEFT JOIN calendar_event_categories c ON c.id = e.category_id
                LEFT JOIN users u ON u.id = e.client_id
                WHERE YEAR(e.event_date) = %s AND MONTH(e.event_date) = %s
                ORDER BY e.event_date, e.event_time
            """, [year, month])

            columns = [col[0] for col in cursor.description]
            events = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # Сериализация дат
        for ev in events:
            if ev.get('event_date'):
                ev['event_date'] = ev['event_date'].strftime('%Y-%m-%d')
            if ev.get('event_time'):
                ev['event_time'] = ev['event_time'].strftime('%H:%M')
            if ev.get('created_at'):
                ev['created_at'] = ev['created_at'].strftime('%Y-%m-%d %H:%M')

        return JsonResponse({'success': True, 'events': events})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e), 'events': []})


@require_POST
@login_required_custom
def api_calendar_event_save(request):
    """API: создать или обновить событие календаря"""
    user = get_current_user(request)
    try:
        data = json.loads(request.body)
        event_id = data.get('id')
        event_date = data.get('event_date')
        event_time = data.get('event_time') or None
        title = data.get('title', '').strip()
        description = data.get('description', '').strip() or None
        category_id = data.get('category_id') or None
        client_id = data.get('client_id') or None

        if not event_date or not title:
            return JsonResponse({'success': False, 'error': 'Дата и название обязательны'})

        with connection.cursor() as cursor:
            if event_id:
                cursor.execute("""
                    UPDATE calendar_events
                    SET event_date=%s, event_time=%s, title=%s, description=%s,
                        category_id=%s, client_id=%s
                    WHERE id=%s
                """, [event_date, event_time, title, description, category_id, client_id, event_id])
            else:
                cursor.execute("""
                    INSERT INTO calendar_events
                        (event_date, event_time, title, description, category_id, client_id, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, [event_date, event_time, title, description, category_id, client_id, user['id']])
                event_id = cursor.lastrowid

        return JsonResponse({'success': True, 'id': event_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@require_POST
@login_required_custom
def api_calendar_event_delete(request, event_id):
    """API: удалить событие"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM calendar_events WHERE id = %s", [event_id])
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required_custom
def api_calendar_categories(request):
    """API: получить список категорий событий"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, code, name, color, icon FROM calendar_event_categories ORDER BY id")
            columns = [col[0] for col in cursor.description]
            categories = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return JsonResponse({'success': True, 'categories': categories})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e), 'categories': []})
