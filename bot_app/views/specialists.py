"""Director specialist management views."""

import json
import logging

from django.db import connection, transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from ..permissions import director_required, get_current_user, login_required_custom
from ..sql_cursor import with_sql_rewrite
from ..sql_helpers import _table_and_column_exist
from .auth import hash_password
from .common import _normalize_phone_digits

connection = with_sql_rewrite(connection)
logger = logging.getLogger(__name__)


def _normalize_partner_phone_digits(value):
    return _normalize_phone_digits(value)


def _ensure_partner_director_chat_for_site_lazy(partner_specialist_id):
    """Lazy-load partner chat setup from the extracted partner views module."""
    from .partner import _ensure_partner_director_chat_for_site

    return _ensure_partner_director_chat_for_site(partner_specialist_id)


@ensure_csrf_cookie
@login_required_custom
@director_required
def specialists_manage(request):
    """Управление специалистами (только директор)"""
    user = get_current_user(request)

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, name, login, profession, phone, tg_username, tg_user_id, max_username, max_user_id, is_active, is_director, created_at
            FROM specialists
            ORDER BY name
        """)
        columns = ['id', 'name', 'login', 'profession', 'phone', 'tg_username', 'tg_user_id', 'max_username', 'max_user_id', 'is_active', 'is_director', 'created_at']
        specialists = [dict(zip(columns, row)) for row in cursor.fetchall()]

    return render(request, 'bot_app/specialists_manage.html', {
        'user': user,
        'specialists': specialists,
    })


@require_POST
def api_specialist_save(request):
    """API: сохранить специалиста"""
    user = get_current_user(request)
    if not user or not user.get('is_director'):
        return JsonResponse({'success': False, 'error': 'Недостаточно прав'})

    try:
        data = json.loads(request.body)
        specialist_id = data.get('id')
        name = data.get('name', '').strip()
        login = data.get('login', '').strip()
        password = data.get('password', '')
        profession = data.get('profession', '').strip()
        phone = data.get('phone', '').strip()
        tg_username = data.get('tg_username', '').strip()
        max_username = data.get('max_username', '').strip()
        is_active = 1 if data.get('is_active', True) else 0
        # Директор определяется по роли (profession=2)
        is_director = 1 if str(profession) == '2' else 0
        role = 'director' if str(profession) == '2' else 'specialist'

        if not name:
            return JsonResponse({'success': False, 'error': 'Имя обязательно'})

        new_specialist_id = None
        phone_changed = False
        with connection.cursor() as cursor:
            if specialist_id:
                # Обновление
                # Для партнёра (profession=5) отслеживаем смену телефона, чтобы перепривязать users
                if str(profession) == '5' and phone:
                    cursor.execute("SELECT phone FROM specialists WHERE id=%s LIMIT 1", [specialist_id])
                    old_row = cursor.fetchone()
                    old_phone_digits = _normalize_partner_phone_digits((old_row[0] if old_row else '') or '')
                    new_phone_digits = _normalize_partner_phone_digits(phone)
                    phone_changed = bool(new_phone_digits and old_phone_digits != new_phone_digits)

                updates = ["name=%s", "profession=%s", "phone=%s", "tg_username=%s", "max_username=%s", "is_director=%s", "is_active=%s", "role=%s"]
                params = [name, profession, phone, tg_username, max_username, is_director, is_active, role]
                if login:
                    updates.append("login=%s")
                    params.append(login)
                if password:
                    updates.append("password_hash=%s")
                    params.append(hash_password(password))
                # При смене телефона партнёра сбрасываем linked_user_id для пересвязки
                if phone_changed:
                    updates.append("linked_user_id=NULL")
                params.append(specialist_id)
                cursor.execute(f"UPDATE specialists SET {', '.join(updates)} WHERE id=%s", params)
                new_specialist_id = int(specialist_id)
            else:
                # Создание нового
                if not login:
                    return JsonResponse({'success': False, 'error': 'Логин обязателен для нового специалиста'})
                if not password:
                    return JsonResponse({'success': False, 'error': 'Пароль обязателен для нового специалиста'})
                cursor.execute("""
                    INSERT INTO specialists (name, login, password_hash, profession, phone, tg_username, max_username, is_director, role, is_active, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """, [name, login, hash_password(password), profession, phone, tg_username, max_username, is_director, role, is_active])
                new_specialist_id = cursor.lastrowid

        # Для партнёра (profession=5) гарантируем linked_user_id и постоянный чат с директором
        if str(profession) == '5' and new_specialist_id:
            try:
                _ensure_partner_director_chat_for_site_lazy(new_specialist_id)
            except Exception as _pe:
                logger.warning("partner setup failed after specialist save id=%s: %s", new_specialist_id, _pe)

        return JsonResponse({'success': True, 'message': 'Сохранено'})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})



def _count_specialist_refs(cursor, specialist_id: int, table_name: str, column_name: str) -> int:
    """Безопасно считает ссылки table.column -> specialists.id."""
    if not _table_and_column_exist(cursor, table_name, column_name):
        return 0
    cursor.execute(f"SELECT COUNT(*) FROM `{table_name}` WHERE `{column_name}` = %s", [specialist_id])
    row = cursor.fetchone()
    return int((row or [0])[0] or 0)


def _get_fk_dependencies(cursor, specialist_id: int) -> list[dict]:
    """Возвращает реальные FK-зависимости в БД для specialists.id с количеством строк."""
    cursor.execute(
        """
        SELECT
            kcu.TABLE_NAME,
            kcu.COLUMN_NAME,
            kcu.CONSTRAINT_NAME,
            rc.DELETE_RULE
          FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
          JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
           AND rc.TABLE_NAME = kcu.TABLE_NAME
           AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
         WHERE kcu.CONSTRAINT_SCHEMA = DATABASE()
           AND kcu.REFERENCED_TABLE_NAME = 'specialists'
           AND kcu.REFERENCED_COLUMN_NAME = 'id'
         ORDER BY kcu.TABLE_NAME, kcu.COLUMN_NAME
        """
    )
    rows = cursor.fetchall() or []
    deps: list[dict] = []
    for table_name, column_name, constraint_name, delete_rule in rows:
        count = _count_specialist_refs(cursor, specialist_id, table_name, column_name)
        if count > 0:
            deps.append({
                'table': table_name,
                'column': column_name,
                'constraint': constraint_name,
                'delete_rule': delete_rule,
                'count': count,
            })
    return deps


@require_POST
def api_specialist_delete(request, specialist_id):
    """API: удаление/деактивация специалиста без потери связанных данных."""
    user = get_current_user(request)
    if not user or not user.get('is_director'):
        return JsonResponse({'success': False, 'error': 'Недостаточно прав'})

    specialist_id = int(specialist_id)
    if user.get('id') == specialist_id:
        return JsonResponse({
            'success': False,
            'error': 'Нельзя удалить или деактивировать текущего авторизованного директора.'
        })

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, name, is_active FROM specialists WHERE id=%s LIMIT 1", [specialist_id])
                row = cursor.fetchone()
                if not row:
                    return JsonResponse({'success': False, 'error': 'Специалист не найден'})

                fk_dependencies = _get_fk_dependencies(cursor, specialist_id)

                logical_checks = [
                    ('support_chats', 'specialist_id', 'Чаты поддержки'),
                    ('support_chat_offers', 'specialist_id', 'Офферы по чатам'),
                    ('tasks', 'created_by', 'Созданные задачи'),
                    ('tasks', 'assignee_id', 'Назначенные задачи'),
                    ('tasks', 'director_id', 'Задачи директора'),
                    ('specialist_promocodes', 'specialist_id', 'Промокоды специалиста'),
                ]
                logical_dependencies = []
                for table_name, column_name, label in logical_checks:
                    count = _count_specialist_refs(cursor, specialist_id, table_name, column_name)
                    if count > 0:
                        logical_dependencies.append({
                            'table': table_name,
                            'column': column_name,
                            'label': label,
                            'count': count,
                        })

                has_dependencies = bool(fk_dependencies or logical_dependencies)

                if has_dependencies:
                    cursor.execute(
                        """
                        UPDATE specialists
                           SET is_active = 0,
                               tg_user_id = NULL,
                               max_user_id = NULL,
                               updated_at = NOW()
                         WHERE id = %s
                        """,
                        [specialist_id],
                    )

                    dependency_hints = []
                    for dep in fk_dependencies:
                        dependency_hints.append(f"{dep['table']}.{dep['column']} ({dep['count']})")
                    for dep in logical_dependencies:
                        dependency_hints.append(f"{dep['label']} ({dep['count']})")

                    return JsonResponse({
                        'success': True,
                        'action': 'deactivated',
                        'message': (
                            'Специалист деактивирован: обнаружены связанные данные, '
                            'поэтому физическое удаление недоступно.'
                        ),
                        'details': {
                            'fk_dependencies': fk_dependencies,
                            'logical_dependencies': logical_dependencies,
                            'summary': ', '.join(dependency_hints[:8]),
                        },
                    })

                cursor.execute("DELETE FROM specialists WHERE id=%s", [specialist_id])
                return JsonResponse({
                    'success': True,
                    'action': 'deleted',
                    'message': 'Специалист удалён (связанные данные не обнаружены).'
                })
    except Exception:
        logger.exception('Ошибка удаления специалиста id=%s', specialist_id)
        return JsonResponse({
            'success': False,
            'error': 'Не удалось выполнить удаление. Проверьте логи сервера.'
        })
