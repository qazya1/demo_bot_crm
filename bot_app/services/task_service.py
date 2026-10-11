"""Task domain services.

Request-independent task helpers extracted from ``bot_app.views.tasks``.
The view layer keeps HTTP parsing/rendering while this module owns shared
task schema helpers and request-independent task operations.
"""

import json
from datetime import datetime

from django.db import connection

from ..sql_cursor import with_sql_rewrite
from ..sql_helpers import _get_table_id_column_type
from ..task_workflow import (
    TaskSnapshot,
    TaskStatus,
    TaskWorkflowError,
    assign_task,
    complete_task,
    create_task_status,
    normalize_status,
    start_task,
)

connection = with_sql_rewrite(connection)


def _ensure_task_clients_table(cursor):
    """Создаёт M2M-таблицу связей задач и клиентов, если её нет."""
    task_id_type = _get_table_id_column_type(cursor, 'tasks', fallback='BIGINT')
    client_id_type = _get_table_id_column_type(cursor, 'users', fallback='BIGINT')

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS task_clients (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            task_id {task_id_type} NOT NULL,
            client_id {client_id_type} NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE INDEX uniq_task_client (task_id, client_id),
            INDEX idx_task_clients_client (client_id),
            CONSTRAINT fk_task_clients_task
                FOREIGN KEY (task_id) REFERENCES tasks(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_task_clients_client
                FOREIGN KEY (client_id) REFERENCES users(id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _ensure_tasks_table():
    """Создаёт таблицу tasks, если не существует."""
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          BIGINT AUTO_INCREMENT PRIMARY KEY,
                title       VARCHAR(500)   NOT NULL,
                description TEXT           NULL,
                source_type VARCHAR(32)    NOT NULL DEFAULT 'manual',
                source_id   BIGINT         NULL,
                dedupe_key  VARCHAR(255)   NULL,
                meta_json   LONGTEXT       NULL,
                director_id BIGINT         NULL,
                created_by  BIGINT         NOT NULL,
                assignee_id BIGINT         NULL,
                client_id   BIGINT         NULL,
                assigned_at DATETIME       NULL,
                completed_at DATETIME      NULL,
                postponed_until DATETIME   NULL,
                deadline    DATETIME       NULL,
                priority    VARCHAR(16)    NOT NULL DEFAULT 'medium',
                status      VARCHAR(32)    NOT NULL DEFAULT 'awaiting_assignment',
                result_comment TEXT        NULL,
                created_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_tasks_assignee  (assignee_id),
                INDEX idx_tasks_status    (status),
                INDEX idx_tasks_deadline  (deadline),
                INDEX idx_tasks_source    (source_type, source_id),
                INDEX idx_tasks_created_by(created_by),
                UNIQUE INDEX idx_tasks_dedupe (dedupe_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        _ensure_tasks_schema(cursor)
        _ensure_task_clients_table(cursor)
        _ensure_task_history_table(cursor)


def _ensure_tasks_schema(cursor):
    """Мягкая миграция таблицы tasks (для существующих БД)."""
    cursor.execute("SHOW COLUMNS FROM tasks LIKE 'dedupe_key'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD COLUMN dedupe_key VARCHAR(255) NULL AFTER source_id")
    cursor.execute("SHOW COLUMNS FROM tasks LIKE 'meta_json'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD COLUMN meta_json LONGTEXT NULL AFTER dedupe_key")

    cursor.execute("SHOW INDEX FROM tasks WHERE Key_name = 'idx_tasks_dedupe'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD UNIQUE INDEX idx_tasks_dedupe (dedupe_key)")
    cursor.execute("SHOW COLUMNS FROM tasks LIKE 'director_id'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD COLUMN director_id BIGINT NULL AFTER meta_json")
    cursor.execute("SHOW COLUMNS FROM tasks LIKE 'client_id'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD COLUMN client_id BIGINT NULL AFTER assignee_id")
    cursor.execute("SHOW COLUMNS FROM tasks LIKE 'assigned_at'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD COLUMN assigned_at DATETIME NULL AFTER client_id")
    cursor.execute("SHOW COLUMNS FROM tasks LIKE 'completed_at'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD COLUMN completed_at DATETIME NULL AFTER assigned_at")
    cursor.execute("SHOW COLUMNS FROM tasks LIKE 'postponed_until'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE tasks ADD COLUMN postponed_until DATETIME NULL AFTER completed_at")
    cursor.execute("UPDATE tasks SET director_id = created_by WHERE director_id IS NULL")
    cursor.execute("UPDATE tasks SET status = 'awaiting_assignment' WHERE status IN ('new', 'waiting')")
    # Исторический статус assigned соответствует каноническому «В работе».
    cursor.execute("UPDATE tasks SET status = 'in_progress' WHERE status = 'assigned'")
    cursor.execute("UPDATE tasks SET status = 'postponed' WHERE status = 'deferred'")
    _ensure_task_clients_table(cursor)


def _service_response(payload, status=None):
    return payload, status


def _task_requires_card_verification_before_complete(*, user, task_row, linked_client_ids):
    """Нужно ли техспециалисту пройти этап проверки карточек перед завершением задачи."""
    if not user or user.get('is_director') or not user.get('is_tech_support'):
        return False
    if not linked_client_ids:
        return False
    assignee_id = task_row.get('assignee_id')
    if not assignee_id or int(assignee_id) != int(user['id']):
        return False

    # Сценарий «директор передал задачу специалисту».
    return bool(task_row.get('director_id')) and bool(task_row.get('assigned_at'))


TASK_CLIENT_ACCESS_STATUSES = (
    'awaiting_assignment',
    'in_progress',
    'postponed',
    'done',
)


def _set_task_clients(task_id, client_ids):
    """Полностью пересохраняет список клиентов для задачи."""
    normalized_ids = _normalize_linked_client_ids(client_ids)
    with connection.cursor() as cursor:
        _ensure_task_clients_table(cursor)
        cursor.execute("DELETE FROM task_clients WHERE task_id = %s", [task_id])
        if normalized_ids:
            values_sql = ", ".join(["(%s, %s)"] * len(normalized_ids))
            params = []
            for client_id in normalized_ids:
                params.extend([task_id, client_id])
            cursor.execute(f"""
                INSERT INTO task_clients (task_id, client_id)
                VALUES {values_sql}
            """, params)


def _get_task_client_ids(task_id):
    with connection.cursor() as cursor:
        _ensure_task_clients_table(cursor)
        cursor.execute("""
            SELECT client_id
            FROM task_clients
            WHERE task_id = %s
            ORDER BY id ASC
        """, [task_id])
        return [int(row[0]) for row in cursor.fetchall()]


def _specialist_has_task_client_access(specialist_id, client_id):
    """Проверяет, прикреплён ли клиент к активной задаче специалиста."""
    if not specialist_id or not client_id:
        return False

    status_placeholders = ", ".join(["%s"] * len(TASK_CLIENT_ACCESS_STATUSES))
    status_sql = f"({status_placeholders})"

    with connection.cursor() as cursor:
        _ensure_task_clients_table(cursor)
        cursor.execute(f"""
            SELECT 1
            FROM task_clients tc
            INNER JOIN tasks t ON t.id = tc.task_id
            WHERE t.assignee_id = %s
              AND t.status IN {status_sql}
              AND tc.client_id = %s
            LIMIT 1
        """, [specialist_id, *TASK_CLIENT_ACCESS_STATUSES, client_id])
        row = cursor.fetchone()
    if row is not None:
        return True

    # Обратная совместимость: старые задачи, где клиенты были только в meta_json.
    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT id, meta_json, client_id
            FROM tasks
            WHERE assignee_id = %s
              AND status IN {status_sql}
              AND meta_json IS NOT NULL
        """, [specialist_id, *TASK_CLIENT_ACCESS_STATUSES])
        rows = cursor.fetchall()

    cid = int(client_id)
    for item in rows:
        task_id = int(item[0])
        raw_meta = item[1]
        legacy_client_id = item[2]
        if not raw_meta:
            continue
        try:
            meta = json.loads(raw_meta)
        except Exception:
            continue
        linked_ids = _resolve_task_linked_client_ids(
            task_id=task_id,
            task_row={'client_id': legacy_client_id},
            meta=meta,
        )
        if cid in linked_ids:
            return True
    return False


def _get_default_director_id():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id
            FROM specialists
            WHERE is_director = 1
            ORDER BY is_active DESC, id ASC
            LIMIT 1
        """)
        row = cursor.fetchone()
        return int(row[0]) if row else 1


def _ensure_task_history_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            task_id BIGINT NOT NULL,
            actor_id BIGINT NULL,
            action_type VARCHAR(64) NOT NULL,
            comment TEXT NULL,
            old_status VARCHAR(32) NULL,
            new_status VARCHAR(32) NULL,
            reminder_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_task_history_task (task_id),
            INDEX idx_task_history_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _add_task_history(cursor, *, task_id, actor_id, action_type, comment=None, old_status=None, new_status=None, reminder_at=None):
    _ensure_task_history_table(cursor)
    cursor.execute("""
        INSERT INTO task_history
            (task_id, actor_id, action_type, comment, old_status, new_status, reminder_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, [task_id, actor_id, action_type, comment, old_status, new_status, reminder_at])


def _build_client_brief(client_id):
    if not client_id:
        return {}
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, first_name, last_name, org_name, inn, phone, email, city, kpp
            FROM users
            WHERE id = %s
            LIMIT 1
        """, [client_id])
        row = cursor.fetchone()
        if not row:
            return {}
        return {
            'client_id': row[0],
            'client_name': (row[3] or f"{row[1] or ''} {row[2] or ''}".strip() or f"Клиент #{row[0]}"),
            'inn': row[4],
            'phone': row[5],
            'email': row[6],
            'city': row[7],
            'kpp': row[8],
        }


def _upsert_task(
    *,
    title,
    description,
    source_type,
    source_id,
    dedupe_key,
    created_by=None,
    assignee_id=None,
    deadline=None,
    priority='medium',
    meta=None,
    ensure_schema=True,
    overwrite_title=False,
):
    """Создать или обновить активную задачу по dedupe_key.
    overwrite_title=False (по умолчанию) — не перезаписывать заголовок/описание,
    если задача уже существует и была отредактирована вручную.
    """
    if ensure_schema:
        _ensure_tasks_table()
    created_by = created_by or _get_default_director_id()
    meta_json = json.dumps(meta or {}, ensure_ascii=False)

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, status, title, description
            FROM tasks
            WHERE dedupe_key = %s
            LIMIT 1
        """, [dedupe_key])
        row = cursor.fetchone()
        if row:
            task_id, status, existing_title, existing_desc = row
            # Не перезаписываем закрытую задачу.
            if status == 'done':
                return int(task_id)
            # Если заголовок был изменён вручную (отличается от автогенерированного),
            # сохраняем пользовательский заголовок.
            final_title = title if overwrite_title or existing_title == title else existing_title
            final_desc = description if overwrite_title or existing_desc == description else existing_desc
            cursor.execute("""
                UPDATE tasks
                SET title = %s,
                    description = %s,
                    source_type = %s,
                    source_id = %s,
                    assignee_id = COALESCE(%s, assignee_id),
                    deadline = COALESCE(%s, deadline),
                    priority = %s,
                    meta_json = %s
                WHERE id = %s
            """, [final_title, final_desc, source_type, source_id, assignee_id, deadline, priority, meta_json, task_id])
            return int(task_id)

        cursor.execute("""
            INSERT INTO tasks
              (title, description, source_type, source_id, dedupe_key, meta_json,
               director_id, created_by, assignee_id, client_id, deadline, priority, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, [title, description, source_type, source_id, dedupe_key, meta_json, created_by, created_by, assignee_id, (meta or {}).get('client_id'), deadline, priority, create_task_status()])
        return int(cursor.lastrowid)


def _normalize_linked_client_ids(raw_ids):
    # Поддерживаем legacy-форматы: CSV-строка и массив объектов клиентов.
    if isinstance(raw_ids, str):
        raw_ids = [part.strip() for part in raw_ids.split(',') if part and part.strip()]
    elif isinstance(raw_ids, dict):
        raw_ids = [raw_ids]
    ids = []
    if not isinstance(raw_ids, (list, tuple)):
        return ids
    for item in raw_ids:
        if isinstance(item, dict):
            item = item.get('id')
        try:
            cid = int(item)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in ids:
            ids.append(cid)
    return ids


def _extract_linked_client_ids_from_meta(meta):
    """Достаёт linked_client_ids из meta_json и нормализует список."""
    if not isinstance(meta, dict):
        return []
    ids = _normalize_linked_client_ids(meta.get('linked_client_ids'))
    # Фоллбек: в ряде карточек сохранялся только linked_clients (массив объектов).
    for cid in _normalize_linked_client_ids(meta.get('linked_clients')):
        if cid not in ids:
            ids.append(cid)
    return ids


def _resolve_task_linked_client_ids(*, task_id, task_row=None, meta=None):
    """Собирает полный список привязанных клиентов: task_clients + meta_json + legacy client_id."""
    resolved_ids = []

    def _append(raw_id):
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            return
        if cid > 0 and cid not in resolved_ids:
            resolved_ids.append(cid)

    for linked_id in _get_task_client_ids(task_id):
        _append(linked_id)
    for linked_id in _extract_linked_client_ids_from_meta(meta):
        _append(linked_id)
    if task_row:
        _append(task_row.get('client_id'))
    if isinstance(meta, dict):
        _append(meta.get('client_id'))

    return resolved_ids


def _fetch_clients_brief(client_ids):
    if not client_ids:
        return []

    placeholders = ", ".join(["%s"] * len(client_ids))
    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT id, inn, org_name, first_name, last_name, phone, email, city, kpp
            FROM users
            WHERE id IN ({placeholders})
        """, client_ids)
        rows = cursor.fetchall()

    by_id = {}
    for row in rows:
        name = (row[2] or '').strip() or f"{(row[3] or '').strip()} {(row[4] or '').strip()}".strip() or f"Клиент #{row[0]}"
        by_id[int(row[0])] = {
            'id': int(row[0]),
            'name': name,
            'first_name': row[3] or '',
            'last_name': row[4] or '',
            'org_name': row[2] or '',
            'inn': row[1] or '',
            'phone': row[5] or '',
            'email': row[6] or '',
            'city': row[7] or '',
            'kpp': row[8] or '',
        }
    return [by_id[cid] for cid in client_ids if cid in by_id]


def create_task_from_payload(user, data):
    """Создать задачу из уже распарсенного payload."""
    _ensure_tasks_table()

    title = (data.get('title') or '').strip()
    if not title:
        return _service_response({'success': False, 'error': 'Заголовок обязателен'})

    description = (data.get('description') or '').strip() or None
    from_event = bool(data.get('from_event'))
    source_type = (data.get('source_type') or 'manual') if from_event else 'manual'
    source_id = data.get('source_id') or None
    assignee_id = data.get('assignee_id') or None
    deadline = data.get('deadline') or None
    priority = data.get('priority', 'medium')
    linked_client_ids = _normalize_linked_client_ids(data.get('linked_client_ids'))

    if source_type not in ('deal', 'renewal', 'order', 'manual'):
        source_type = 'manual'
    if priority not in ('low', 'medium', 'high'):
        priority = 'medium'
    if assignee_id not in (None, ''):
        try:
            assignee_id = int(assignee_id)
        except (TypeError, ValueError):
            return _service_response({'success': False, 'error': 'Некорректный исполнитель'}, status=400)
    else:
        assignee_id = None

    dedupe_key = None
    if from_event and source_id:
        dedupe_key = f"event:{source_type}:{source_id}:{title[:80]}"
    meta = {'created_from_event': from_event} if from_event else {}
    if linked_client_ids:
        meta['linked_client_ids'] = linked_client_ids
        meta['linked_clients'] = _fetch_clients_brief(linked_client_ids)
    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
    new_status = create_task_status()
    assigned_at = None
    if assignee_id:
        try:
            new_status = assign_task(
                task=TaskSnapshot(id=0, status=new_status, assignee_id=None),
                specialist_id=assignee_id,
                actor_is_director=True,
            )
        except TaskWorkflowError as exc:
            return _service_response({'success': False, 'error': str(exc)}, status=400)
        assigned_at = datetime.now()

    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO tasks
              (title, description, source_type, source_id, dedupe_key, meta_json,
               director_id, created_by, assignee_id, client_id, assigned_at, deadline, priority, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, [
            title, description, source_type, source_id, dedupe_key, meta_json,
            user['id'], user['id'], assignee_id, (linked_client_ids[0] if linked_client_ids else None),
            assigned_at, deadline, priority, new_status
        ])
        task_id = cursor.lastrowid
    _set_task_clients(task_id, linked_client_ids)

    return _service_response({'success': True, 'task_id': task_id})


def update_task_from_payload(user, task_id, data):
    """Обновить задачу из уже распарсенного payload."""
    _ensure_tasks_table()

    is_director = user.get('is_director')

    with connection.cursor() as cursor:
        # Проверяем задачу
        cursor.execute("""
            SELECT id, assignee_id, status, meta_json, director_id, assigned_at, client_id
            FROM tasks
            WHERE id = %s
        """, [task_id])
        row = cursor.fetchone()
        if not row:
            return _service_response({'success': False, 'error': 'Задача не найдена'}, status=404)

        action = (data.get('action') or '').strip()
        current_status = normalize_status(row[2])
        current_meta_raw = row[3]
        try:
            current_meta = json.loads(current_meta_raw) if current_meta_raw else {}
        except Exception:
            current_meta = {}
        current_linked_ids = _resolve_task_linked_client_ids(
            task_id=task_id,
            task_row={'client_id': row[6]},
            meta=current_meta,
        )
        snapshot = TaskSnapshot(id=int(row[0]), status=current_status, assignee_id=row[1])
        requires_card_verification_before_complete = _task_requires_card_verification_before_complete(
            user=user,
            task_row={
                'assignee_id': row[1],
                'director_id': row[4],
                'assigned_at': row[5],
            },
            linked_client_ids=current_linked_ids,
        )

        # Явная операция назначения задачи специалисту из карточки директора.
        if action == 'assign':
            if not is_director:
                return _service_response({'success': False, 'error': 'Назначение доступно только директору'}, status=403)
            assignee_id = data.get('assignee_id')
            if assignee_id in (None, ''):
                return _service_response({'success': False, 'error': 'Выберите специалиста перед постановкой задачи'})
            try:
                assignee_id = int(assignee_id)
            except (TypeError, ValueError):
                return _service_response({'success': False, 'error': 'Некорректный исполнитель'}, status=400)

            # Повторное назначение того же специалиста считаем идемпотентным.
            if snapshot.assignee_id and int(snapshot.assignee_id) == assignee_id:
                return _service_response({'success': True, 'assigned': True, 'status': current_status})

            if current_status == TaskStatus.DONE.value:
                return _service_response({'success': False, 'error': 'Нельзя переназначить выполненную задачу'}, status=400)

            # Для задач без исполнителя используем каноничный workflow assign.
            # Для уже назначенных/в работе выполняем явное переназначение.
            if not snapshot.assignee_id:
                try:
                    new_status = assign_task(task=snapshot, specialist_id=assignee_id, actor_is_director=True)
                except TaskWorkflowError as exc:
                    return _service_response({'success': False, 'error': str(exc)}, status=400)
            else:
                new_status = current_status
                if current_status == TaskStatus.AWAITING_ASSIGNMENT.value:
                    new_status = TaskStatus.IN_PROGRESS.value

            cursor.execute("""
                UPDATE tasks
                SET assignee_id = %s,
                    status = %s,
                    assigned_at = NOW()
                WHERE id = %s
            """, [assignee_id, new_status, task_id])
            if 'linked_client_ids' in data:
                linked_client_ids = _normalize_linked_client_ids(data.get('linked_client_ids'))
                if linked_client_ids:
                    current_meta['linked_client_ids'] = linked_client_ids
                    current_meta['linked_clients'] = _fetch_clients_brief(linked_client_ids)
                else:
                    current_meta.pop('linked_client_ids', None)
                    current_meta.pop('linked_clients', None)
                cursor.execute(
                    "UPDATE tasks SET meta_json = %s WHERE id = %s",
                    [json.dumps(current_meta, ensure_ascii=False) if current_meta else None, task_id],
                )
                _set_task_clients(task_id, linked_client_ids)
            return _service_response({'success': True, 'assigned': True, 'status': new_status})

        if action == 'complete':
            if not snapshot.assignee_id or int(snapshot.assignee_id) != int(user['id']):
                return _service_response({'success': False, 'error': 'Завершать задачу может только назначенный специалист'}, status=403)
            if requires_card_verification_before_complete and not bool(data.get('cards_verified')):
                return _service_response({
                    'success': False,
                    'error': 'Перед завершением задачи подтвердите проверку карточек клиентов/компаний',
                    'requires_card_verification': True,
                }, status=400)
            try:
                new_status = complete_task(task=snapshot, actor_id=int(user['id']))
            except TaskWorkflowError as exc:
                return _service_response({'success': False, 'error': str(exc)}, status=400)
            cursor.execute("""
                UPDATE tasks
                SET status = %s,
                    completed_at = NOW()
                WHERE id = %s
            """, [new_status, task_id])
            _add_task_history(
                cursor,
                task_id=task_id,
                actor_id=user['id'],
                action_type='complete',
                old_status=current_status,
                new_status=new_status,
                comment=(data.get('result_comment') or '').strip() or None,
            )
            return _service_response({'success': True, 'status': new_status})

        if action == 'postpone':
            if not snapshot.assignee_id or int(snapshot.assignee_id) != int(user['id']):
                if not is_director:
                    return _service_response({'success': False, 'error': 'Отложить задачу может только исполнитель или директор'}, status=403)
            postpone_until = (data.get('postpone_until') or '').strip()
            transfer_comment = (data.get('transfer_comment') or '').strip()
            if not postpone_until:
                return _service_response({'success': False, 'error': 'Укажите дату и время напоминания'}, status=400)
            if not transfer_comment:
                return _service_response({'success': False, 'error': 'Комментарий обязателен при отложении'}, status=400)
            cursor.execute("""
                UPDATE tasks
                SET status = 'postponed',
                    postponed_until = %s,
                    result_comment = %s,
                    completed_at = NULL
                WHERE id = %s
            """, [postpone_until, transfer_comment, task_id])
            _add_task_history(
                cursor,
                task_id=task_id,
                actor_id=user['id'],
                action_type='postpone',
                old_status=current_status,
                new_status='postponed',
                comment=transfer_comment,
                reminder_at=postpone_until,
            )
            return _service_response({'success': True, 'status': 'postponed'})

        if action == 'transfer':
            if not snapshot.assignee_id or int(snapshot.assignee_id) != int(user['id']):
                if not is_director:
                    return _service_response({'success': False, 'error': 'Перенос доступен только исполнителю или директору'}, status=403)
            new_status = normalize_status(data.get('status'))
            transfer_comment = (data.get('transfer_comment') or '').strip()
            if new_status not in (TaskStatus.IN_PROGRESS.value, TaskStatus.DONE.value, TaskStatus.POSTPONED.value):
                return _service_response({'success': False, 'error': 'Недопустимый статус для переноса'}, status=400)
            if not transfer_comment:
                return _service_response({'success': False, 'error': 'Комментарий обязателен при переносе'}, status=400)
            if (
                new_status == TaskStatus.DONE.value
                and requires_card_verification_before_complete
                and not bool(data.get('cards_verified'))
            ):
                return _service_response({
                    'success': False,
                    'error': 'Перед завершением задачи подтвердите проверку карточек клиентов/компаний',
                    'requires_card_verification': True,
                }, status=400)

            sets_sql = ["status = %s", "postponed_until = NULL", "result_comment = %s"]
            params_sql = [new_status, transfer_comment]
            if new_status == TaskStatus.DONE.value:
                sets_sql.append("completed_at = COALESCE(completed_at, NOW())")
            elif current_status == TaskStatus.DONE.value:
                sets_sql.append("completed_at = NULL")

            cursor.execute(f"UPDATE tasks SET {', '.join(sets_sql)} WHERE id = %s", params_sql + [task_id])
            _add_task_history(
                cursor,
                task_id=task_id,
                actor_id=user['id'],
                action_type='transfer',
                old_status=current_status,
                new_status=new_status,
                comment=transfer_comment,
            )
            return _service_response({'success': True, 'status': new_status})

        # Базовая RBAC-проверка на редактирование карточки
        if not is_director and row[1] != user['id']:
            return _service_response({'success': False, 'error': 'Нет доступа'}, status=403)

        sets = []
        params = []

        if is_director:
            # Директор может менять всё
            for field in ('title', 'description', 'deadline', 'priority', 'result_comment'):
                if field in data:
                    val = data[field]
                    if field == 'priority' and val not in ('low', 'medium', 'high'):
                        continue
                    if field == 'title' and not (val or '').strip():
                        return _service_response({'success': False, 'error': 'Заголовок обязателен'}, status=400)
                    sets.append(f"{field} = %s")
                    params.append(val if val != '' else None)

            if 'assignee_id' in data:
                raw_assignee_id = data.get('assignee_id')
                assignee_id = int(raw_assignee_id) if raw_assignee_id not in (None, '') else None
                if assignee_id and not snapshot.assignee_id:
                    # Назначение новой задачи — сразу «В работе»
                    try:
                        assigned_status = assign_task(task=snapshot, specialist_id=assignee_id, actor_is_director=True)
                    except TaskWorkflowError as exc:
                        return _service_response({'success': False, 'error': str(exc)}, status=400)
                    sets.append("assignee_id = %s")
                    params.append(assignee_id)
                    sets.append("status = %s")
                    params.append(assigned_status)
                    sets.append("assigned_at = COALESCE(assigned_at, NOW())")
                elif assignee_id and snapshot.assignee_id and int(snapshot.assignee_id) != assignee_id:
                    # Переназначение — задача остаётся «В работе»
                    if current_status == TaskStatus.DONE.value:
                        return _service_response({'success': False, 'error': 'Нельзя переназначить выполненную задачу'}, status=400)
                    sets.append("assignee_id = %s")
                    params.append(assignee_id)
                    if current_status == TaskStatus.AWAITING_ASSIGNMENT.value:
                        sets.append("status = %s")
                        params.append(TaskStatus.IN_PROGRESS.value)
                        sets.append("assigned_at = COALESCE(assigned_at, NOW())")
                elif assignee_id is None and snapshot.assignee_id:
                    # Снятие исполнителя
                    if current_status == TaskStatus.DONE.value:
                        return _service_response({'success': False, 'error': 'Нельзя снять исполнителя с выполненной задачи'}, status=400)
                    sets.append("assignee_id = NULL")
                    if current_status in (TaskStatus.IN_PROGRESS.value,):
                        sets.append("status = %s")
                        params.append(TaskStatus.AWAITING_ASSIGNMENT.value)
                        sets.append("completed_at = NULL")

            if 'linked_client_ids' in data:
                linked_client_ids = _normalize_linked_client_ids(data.get('linked_client_ids'))
                if linked_client_ids:
                    current_meta['linked_client_ids'] = linked_client_ids
                    current_meta['linked_clients'] = _fetch_clients_brief(linked_client_ids)
                else:
                    current_meta.pop('linked_client_ids', None)
                    current_meta.pop('linked_clients', None)
                sets.append("meta_json = %s")
                params.append(json.dumps(current_meta, ensure_ascii=False) if current_meta else None)
                current_linked_ids = linked_client_ids
        else:
            # Специалист — только статус и комментарий
            requested_status = normalize_status(data.get('status')) if 'status' in data else None
            # Явный старт работы специалистом: awaiting_assignment -> in_progress
            if requested_status == TaskStatus.IN_PROGRESS.value:
                if not snapshot.assignee_id or int(snapshot.assignee_id) != int(user['id']):
                    return _service_response({'success': False, 'error': 'Перевод в работу доступен только назначенному специалисту'}, status=403)
                if current_status in (TaskStatus.AWAITING_ASSIGNMENT.value, TaskStatus.ASSIGNED.value):
                    try:
                        started_status = start_task(task=snapshot, actor_id=int(user['id']))
                    except TaskWorkflowError as exc:
                        return _service_response({'success': False, 'error': str(exc)}, status=400)
                    sets.append("status = %s")
                    params.append(started_status)
                    sets.append("assigned_at = COALESCE(assigned_at, NOW())")
            if requested_status == TaskStatus.DONE.value:
                if not snapshot.assignee_id or int(snapshot.assignee_id) != int(user['id']):
                    return _service_response({'success': False, 'error': 'Завершать задачу может только назначенный специалист'}, status=403)
                if requires_card_verification_before_complete and not bool(data.get('cards_verified')):
                    return _service_response({
                        'success': False,
                        'error': 'Перед завершением задачи подтвердите проверку карточек клиентов/компаний',
                        'requires_card_verification': True,
                    }, status=400)
                if current_status in (TaskStatus.AWAITING_ASSIGNMENT.value, TaskStatus.ASSIGNED.value):
                    try:
                        current_status = start_task(task=snapshot, actor_id=int(user['id']))
                    except TaskWorkflowError as exc:
                        return _service_response({'success': False, 'error': str(exc)}, status=400)
                    sets.append("status = %s")
                    params.append(current_status)
                    sets.append("assigned_at = COALESCE(assigned_at, NOW())")
                    snapshot = TaskSnapshot(id=snapshot.id, status=current_status, assignee_id=snapshot.assignee_id)
                try:
                    new_status = complete_task(task=snapshot, actor_id=int(user['id']))
                except TaskWorkflowError as exc:
                    return _service_response({'success': False, 'error': str(exc)}, status=400)
                sets.append("status = %s")
                params.append(new_status)
                sets.append("completed_at = COALESCE(completed_at, NOW())")
            if requested_status == TaskStatus.POSTPONED.value:
                return _service_response({'success': False, 'error': 'Используйте кнопку «Отложить» для постановки паузы'}, status=400)
            if 'result_comment' in data:
                sets.append("result_comment = %s")
                params.append(data['result_comment'] or None)

        if not sets:
            return _service_response({'success': False, 'error': 'Нечего обновлять'})

        params.append(task_id)
        cursor.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = %s", params)
        if 'status = %s' in sets:
            try:
                idx = sets.index('status = %s')
                new_status_for_history = params[idx]
            except Exception:
                new_status_for_history = None
            if new_status_for_history and new_status_for_history != current_status:
                _add_task_history(
                    cursor,
                    task_id=task_id,
                    actor_id=user['id'],
                    action_type='status_update',
                    old_status=current_status,
                    new_status=new_status_for_history,
                    comment=(data.get('result_comment') or '').strip() or None,
                )
        if is_director and 'linked_client_ids' in data:
            _set_task_clients(task_id, current_linked_ids)

    return _service_response({'success': True})


def delete_task(user, task_id):
    """Удалить задачу."""
    _ensure_tasks_table()
    with connection.cursor() as cursor:
        _ensure_task_clients_table(cursor)
        cursor.execute("DELETE FROM task_clients WHERE task_id = %s", [task_id])
        cursor.execute("DELETE FROM tasks WHERE id = %s", [task_id])
        deleted = cursor.rowcount
    return _service_response({'success': bool(deleted)})
