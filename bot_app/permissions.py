"""Access helpers and decorators for CRM views."""

from functools import wraps

from django.db import connection
from django.shortcuts import redirect

from .sql_cursor import with_sql_rewrite
from .view_access_helpers import (
    is_partner_client_accessible,
    specialist_has_task_client_access,
)

connection = with_sql_rewrite(connection)


def get_current_user(request):
    """Получить текущего авторизованного пользователя из сессии"""
    specialist_id = request.session.get('specialist_id')
    if not specialist_id:
        return None

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, name, login, is_director, profession, phone, tg_username, tg_user_id, max_user_id
            FROM specialists
            WHERE id = %s
        """, [specialist_id])
        row = cursor.fetchone()
        if row:
            is_director = bool(row[3])
            profession = row[4]
            is_tech_support = (profession == 1)
            is_partner = (profession == 5)
            return {
                'id': row[0],
                'name': row[1],
                'login': row[2],
                'is_director': is_director,
                'is_specialist': (not is_director and not is_partner),
                'is_partner': is_partner,
                'is_tech_support': is_tech_support,
                # Полный CRM-доступ (вся БД) — только у директора.
                'has_crm_access': is_director,
                'profession': profession,
                'phone': row[5],
                'tg_username': row[6],
                'tg_user_id': row[7],
                'max_user_id': row[8],
            }
    return None


def login_required_custom(view_func):
    """Декоратор проверки авторизации"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.session.get('specialist_id'):
            return redirect('bot_app:login')
        return view_func(request, *args, **kwargs)
    return wrapper


def director_required(view_func):
    """Декоратор - только для директора"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = get_current_user(request)
        if not user or not user.get('is_director'):
            return redirect('bot_app:specialist_panel')
        return view_func(request, *args, **kwargs)
    return wrapper


def crm_access_required(view_func):
    """Декоратор - для директора и техподдержки (доступ к БД/CRM)"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = get_current_user(request)
        if not user or not user.get('has_crm_access'):
            return redirect('bot_app:specialist_panel')
        return view_func(request, *args, **kwargs)
    return wrapper


def tech_or_director_required(view_func):
    """Декоратор — только для тех. специалиста (profession=1) или директора"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = get_current_user(request)
        if not user or (not user.get('is_director') and not user.get('is_tech_support')):
            return redirect('bot_app:specialist_panel')
        return view_func(request, *args, **kwargs)
    return wrapper


def partner_required(view_func):
    """Декоратор — только для партнёра (profession=5)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = get_current_user(request)
        if not user or not user.get('is_partner'):
            return redirect('bot_app:specialist_panel')
        return view_func(request, *args, **kwargs)
    return wrapper


def _specialist_has_client(specialist_id, client_id):
    """Проверяет, есть ли у специалиста активный рабочий чат с клиентом."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 1 FROM support_chats
            WHERE specialist_id = %s
              AND client_user_id = %s
              AND status IN ('pending', 'active', 'consultation')
            LIMIT 1
        """, [specialist_id, client_id])
        return cursor.fetchone() is not None


def _check_specialist_client_access(user, client_id):
    """Проверяет доступ к клиенту:
    - Директор — всегда
    - Тех. специалист — при активном чате или если он создал клиента
    - Бухгалтер/Консультант/Разрешительная документация — только при активном чате (без создания)
    """
    if user.get('is_director'):
        return True
    if user.get('is_partner'):
        return is_partner_client_accessible(user['id'], client_id)
    # Любой специалист может видеть клиента, если есть активный чат
    if _specialist_has_client(user['id'], client_id):
        return True
    # Любой специалист может видеть/редактировать клиента, если клиент прикреплён к его задаче
    if specialist_has_task_client_access(user['id'], client_id):
        return True
    # Только тех. специалист может видеть клиентов, которых он создал
    if user.get('is_tech_support'):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM users WHERE id = %s AND added_by_specialist_id = %s LIMIT 1", [client_id, user['id']])
            return cursor.fetchone() is not None
    return False
