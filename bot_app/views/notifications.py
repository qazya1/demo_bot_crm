"""Notification views and helpers."""

import calendar
from datetime import date, datetime

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from ..permissions import director_required, get_current_user, login_required_custom
from ..services.notification_service import (
    _ensure_notification_tables,
    _get_unread_notif_count,
    _notifications_page_context,
    _sync_kkt_expiry_notifications,
    dismiss_notification,
    get_notifications_count,
    mark_notification_read,
    mark_notifications_read_all,
)
from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)



@ensure_csrf_cookie
@login_required_custom
@director_required
def notifications_list(request):
    """Страница списка уведомлений"""
    return render(request, 'bot_app/notifications/list.html', _notifications_page_context(request, renewal_only=False))


@ensure_csrf_cookie
@login_required_custom
@director_required
def notifications_renewal(request):
    """Страница уведомлений о продлении сроков"""
    return render(request, 'bot_app/notifications/list.html', _notifications_page_context(request, renewal_only=True))


@ensure_csrf_cookie
@login_required_custom
@director_required
def notifications_calendar(request):
    """Страница календаря уведомлений (сроки)"""
    user = get_current_user(request)

    # Получаем год и месяц из параметров или текущие
    import calendar
    today = date.today()
    year = int(request.GET.get('year', today.year))
    month = int(request.GET.get('month', today.month))

    # Вычисляем предыдущий и следующий месяц
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    # Получаем сроки из client_kkt
    with connection.cursor() as cursor:
        # Все сроки из ККТ за месяц
        cursor.execute("""
            SELECT
                k.id AS kkt_id, k.kkt_slot, k.address,
                k.ofd_expires_at, k.fn_expires_at, k.mark_expires_at,
                k.tspiot_tariff_expires_at, k.ecp_expires_at,
                u.id AS client_id, u.first_name, u.last_name, u.inn, u.phone
            FROM client_kkt k
            JOIN users u ON u.id = k.client_id
            WHERE (
                (k.ofd_expires_at IS NOT NULL AND YEAR(k.ofd_expires_at) = %s AND MONTH(k.ofd_expires_at) = %s)
                OR (k.fn_expires_at IS NOT NULL AND YEAR(k.fn_expires_at) = %s AND MONTH(k.fn_expires_at) = %s)
                OR (k.mark_expires_at IS NOT NULL AND YEAR(k.mark_expires_at) = %s AND MONTH(k.mark_expires_at) = %s)
                OR (k.tspiot_tariff_expires_at IS NOT NULL AND YEAR(k.tspiot_tariff_expires_at) = %s AND MONTH(k.tspiot_tariff_expires_at) = %s)
                OR (k.ecp_expires_at IS NOT NULL AND YEAR(k.ecp_expires_at) = %s AND MONTH(k.ecp_expires_at) = %s)
            )
            ORDER BY u.first_name
        """, [year, month] * 5)

        columns = [col[0] for col in cursor.description]
        kkt_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    # Группируем по дням
    deadlines_by_day = {}
    deadline_types = {
        'ofd_expires_at': ('ОФД', 'ofd'),
        'fn_expires_at': ('ФН', 'fn'),
        'mark_expires_at': ('Маркировка', 'marking'),
        'tspiot_tariff_expires_at': ('ТСПИоТ', 'tspiot'),
        'ecp_expires_at': ('ЭЦП', 'ecp'),
    }

    for row in kkt_rows:
        client_name = f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
        client_info = client_name or row['inn'] or row['phone'] or f"ID {row['client_id']}"

        for field, (label, code) in deadline_types.items():
            if row.get(field):
                d = row[field]
                if isinstance(d, datetime):
                    d = d.date()
                day_key = d.day

                if day_key not in deadlines_by_day:
                    deadlines_by_day[day_key] = []

                deadlines_by_day[day_key].append({
                    'type': code,
                    'label': label,
                    'date': d,
                    'client_id': row['client_id'],
                    'client_name': client_info,
                    'kkt_slot': row['kkt_slot'],
                    'address': row['address'],
                })

    # Генерируем календарь
    cal = calendar.Calendar(firstweekday=0)  # Понедельник
    month_days = cal.monthdayscalendar(year, month)
    month_name = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'][month]

    return render(request, 'bot_app/notifications/calendar.html', {
        'user': user,
        'year': year,
        'month': month,
        'month_name': month_name,
        'month_days': month_days,
        'deadlines_by_day': deadlines_by_day,
        'today': today,
        'prev_year': prev_year,
        'prev_month': prev_month,
        'next_year': next_year,
        'next_month': next_month,
    })


@require_POST
@login_required_custom
def api_notification_read(request, notification_id):
    """Отметить уведомление как прочитанное"""
    user = get_current_user(request)
    return JsonResponse(mark_notification_read(user, notification_id))


@require_POST
@login_required_custom
def api_notification_dismiss(request, notification_id):
    """Отклонить (скрыть) уведомление"""
    user = get_current_user(request)
    return JsonResponse(dismiss_notification(user, notification_id))


@require_POST
@login_required_custom
def api_notifications_read_all(request):
    """Отметить все уведомления как прочитанные (с учётом scope: renewal/general/all)"""
    user = get_current_user(request)
    scope = request.POST.get('scope', 'all')
    return JsonResponse(mark_notifications_read_all(user, scope))


@login_required_custom
def api_notifications_count(request):
    """API: получить количество непрочитанных уведомлений (для бейджа)"""
    return JsonResponse(get_notifications_count())
