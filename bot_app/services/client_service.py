"""Client domain services.

Request-independent client helpers extracted from ``bot_app.views.clients``.
The view layer keeps decorators, request parsing, access checks, and HTTP
response construction while this module owns client search and notes SQL
operations plus payload creation.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from django.db import IntegrityError, connection

from ..sql_cursor import with_sql_rewrite
from .order_service import _ensure_orders_manual_columns
from .task_service import _build_client_brief, _upsert_task

connection = with_sql_rewrite(connection)
logger = logging.getLogger(__name__)


def build_client_display(row):
    """Формирует читаемое отображение клиента для UI."""
    parts = []
    name = (row.get('org_name') or '').strip()
    if not name:
        name = f"{(row.get('first_name') or '').strip()} {(row.get('last_name') or '').strip()}".strip()
    if name:
        parts.append(name)
    if row.get('inn'):
        parts.append(f"ИНН {row['inn']}")
    if row.get('phone'):
        parts.append(row['phone'])
    return ' • '.join(parts) if parts else f"Клиент #{row.get('id', '?')}"


def _ensure_client_notes_table():
    """Создаёт таблицу client_notes если не существует"""
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS client_notes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                client_id INT NOT NULL,
                author_id INT NOT NULL,
                author_name VARCHAR(255) NOT NULL,
                note_type ENUM('note', 'deal') NOT NULL DEFAULT 'note',
                product VARCHAR(255) DEFAULT NULL,
                amount DECIMAL(12,2) DEFAULT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                KEY idx_client (client_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)


def search_clients(query):
    """Найти клиентов по имени, телефону или ИНН и вернуть API payload."""
    query = (query or '').strip()
    if not query or len(query) < 2:
        return {'clients': []}

    like = f'%{query}%'
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, inn, org_name, first_name, last_name, phone, email, city
            FROM users
            WHERE inn LIKE %s
               OR org_name LIKE %s
               OR first_name LIKE %s
               OR last_name LIKE %s
               OR CONCAT_WS(' ', COALESCE(first_name, ''), COALESCE(last_name, '')) LIKE %s
               OR CONCAT_WS(' ', COALESCE(last_name, ''), COALESCE(first_name, '')) LIKE %s
               OR phone LIKE %s
               OR email LIKE %s
            ORDER BY
                CASE WHEN inn = %s THEN 0
                     WHEN org_name = %s THEN 1
                     WHEN first_name = %s OR last_name = %s THEN 2
                     WHEN phone = %s THEN 3
                     ELSE 4
                END,
                id DESC
            LIMIT 30
        """, [
            like, like, like, like, like, like, like, like,
            query.replace('%', ''), query.replace('%', ''), query.replace('%', ''), query.replace('%', ''),
            query.replace('%', '')
        ])
        columns = ['id', 'inn', 'org_name', 'first_name', 'last_name', 'phone', 'email', 'city']
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    result = []
    for r in rows:
        name = (r.get('org_name') or '').strip()
        if not name:
            name = f"{(r.get('first_name') or '').strip()} {(r.get('last_name') or '').strip()}".strip()
        if not name:
            name = f"Клиент #{r['id']}"
        result.append({
            'id': r['id'],
            'name': name,
            'inn': r.get('inn') or '',
            'phone': r.get('phone') or '',
            'email': r.get('email') or '',
            'display': build_client_display(r),
        })

    return {'clients': result}


def get_client_notes(client_id):
    """Получить заметки и сделки клиента."""
    _ensure_client_notes_table()
    _ensure_orders_manual_columns()
    try:
        notes = []
        with connection.cursor() as cursor:
            # Заметки и старые сделки из client_notes
            cursor.execute("""
                SELECT id, note_type, product, amount, description,
                       author_name, created_at
                FROM client_notes
                WHERE client_id = %s
                ORDER BY created_at DESC
            """, [client_id])
            columns = [col[0] for col in cursor.description]
            for row in cursor.fetchall():
                note = dict(zip(columns, row))
                note['source_table'] = 'client_notes'
                if note['created_at']:
                    note['created_at'] = note['created_at'].strftime('%d.%m.%Y %H:%M')
                if note['amount']:
                    note['amount'] = float(note['amount'])
                notes.append(note)

            # Ручные сделки из orders (source='manual')
            cursor.execute("""
                SELECT o.id, o.amount, o.status, o.description, o.author_name,
                       o.created_at, o.paid_at,
                       GROUP_CONCAT(DISTINCT oi.legacy_item_title SEPARATOR ', ') AS product
                FROM orders o
                LEFT JOIN order_items oi ON oi.order_id = o.id
                WHERE o.user_id = %s AND o.source = 'manual'
                GROUP BY o.id
                ORDER BY o.created_at DESC
            """, [client_id])
            for row in cursor.fetchall():
                cols = [col[0] for col in cursor.description]
                deal = dict(zip(cols, row))
                notes.append({
                    'id': deal['id'],
                    'note_type': 'deal',
                    'product': deal['product'] or '',
                    'amount': float(deal['amount']) if deal['amount'] else None,
                    'description': deal['description'] or '',
                    'author_name': deal['author_name'] or '',
                    'created_at': deal['created_at'].strftime('%d.%m.%Y %H:%M') if deal['created_at'] else '',
                    'source_table': 'orders',
                    'status': deal['status'],
                    'paid_at': deal['paid_at'].strftime('%d.%m.%Y %H:%M') if deal['paid_at'] else None,
                })

        # Сортировка по дате (все вместе)
        notes.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        return {'success': True, 'notes': notes}
    except Exception as e:
        return {'success': False, 'error': str(e), 'notes': []}


def save_client_note(user, client_id, data):
    """Создать заметку или сделку клиента."""
    _ensure_client_notes_table()
    _ensure_orders_manual_columns()
    try:
        note_type = data.get('note_type', 'note')
        product = data.get('product', '').strip()
        amount = data.get('amount')
        description = data.get('description', '').strip()
        status = data.get('status', 'CONFIRMED')

        if not description and not product:
            return {'success': False, 'error': 'Заполните описание или продукт'}

        if note_type not in ('note', 'deal'):
            note_type = 'note'

        if note_type == 'deal':
            # Сделки сохраняем в orders с source='manual'
            try:
                amount_int = int(float(amount)) if amount else 0
            except (ValueError, TypeError):
                amount_int = 0
            if status not in ('INIT', 'CONFIRMED', 'ADVANCE'):
                status = 'CONFIRMED'

            now = datetime.now()
            paid_at = now if status in ('CONFIRMED', 'ADVANCE') else None
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO orders (user_id, amount, status, source, description, author_name, created_at, updated_at, paid_at)
                    VALUES (%s, %s, %s, 'manual', %s, %s, %s, %s, %s)
                """, [client_id, amount_int, status, description or None, user['name'], now, now, paid_at])
                order_id = cursor.lastrowid

                if product:
                    cursor.execute("""
                        INSERT INTO order_items (order_id, qty, price, created_at, legacy_item_type, legacy_item_title)
                        VALUES (%s, 1, %s, %s, 'manual', %s)
                    """, [order_id, amount_int, now, product])
            client_brief = _build_client_brief(client_id)
            _upsert_task(
                title=f"Сделка #{order_id} — {client_brief.get('client_name') or f'Клиент #{client_id}'}",
                description=(description or product or f"Сделка #{order_id}"),
                source_type='deal',
                source_id=order_id,
                dedupe_key=f"deal:order:{order_id}",
                assignee_id=user['id'],
                priority='high',
                meta={
                    **client_brief,
                    'channel': 'manual',
                    'author': user['name'],
                    'amount': amount_int,
                    'payment_status': status,
                },
            )
        else:
            # Заметки по-прежнему в client_notes
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO client_notes (client_id, author_id, author_name, note_type, product, amount, description)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, [client_id, user['id'], user['name'], note_type,
                      product or None, amount or None, description or None])

        return {'success': True, 'message': 'Сохранено'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def delete_client_note(user, client_id, note_id, source_table='client_notes'):
    """Удалить заметку или ручную сделку клиента."""
    _ensure_client_notes_table()
    try:
        with connection.cursor() as cursor:
            if source_table == 'orders':
                # Удаляем ручную сделку из orders
                cursor.execute("DELETE FROM order_items WHERE order_id = %s", [note_id])
                cursor.execute("DELETE FROM orders WHERE id = %s AND user_id = %s AND source = 'manual'", [note_id, client_id])
            else:
                cursor.execute("DELETE FROM client_notes WHERE id = %s AND client_id = %s", [note_id, client_id])
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def save_custom_field(user, client_id, data):
    """Сохранить кастомное поле клиента и вернуть API payload."""
    try:
        field_name = data.get('field_name', '').strip()
        field_value = data.get('field_value', '').strip()
        field_id = data.get('field_id')  # Если есть - обновляем, если нет - создаём

        if not field_name:
            return {'success': False, 'error': 'Название поля обязательно'}

        with connection.cursor() as cursor:
            # Проверяем существует ли клиент
            cursor.execute("SELECT id FROM users WHERE id = %s", [client_id])
            if not cursor.fetchone():
                return {'success': False, 'error': 'Клиент не найден'}

            if field_id:
                # Обновляем существующее поле
                cursor.execute("""
                    UPDATE user_custom_fields
                    SET field_name = %s, field_value = %s, updated_at = NOW()
                    WHERE id = %s AND user_id = %s
                """, [field_name, field_value, field_id, client_id])
                return {'success': True, 'message': 'Поле обновлено', 'field_id': field_id}
            else:
                # Создаём новое поле
                cursor.execute("""
                    INSERT INTO user_custom_fields (user_id, field_name, field_value, created_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE field_value = VALUES(field_value), updated_at = NOW()
                """, [client_id, field_name, field_value])
                # Получаем ID созданного поля
                cursor.execute("SELECT LAST_INSERT_ID()")
                new_id = cursor.fetchone()[0]
                return {'success': True, 'message': 'Поле добавлено', 'field_id': new_id}

    except Exception as e:
        return {'success': False, 'error': str(e)}


def delete_custom_field(user, client_id, field_id):
    """Удалить кастомное поле клиента и вернуть API payload."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                DELETE FROM user_custom_fields WHERE id = %s AND user_id = %s
            """, [field_id, client_id])

        return {'success': True, 'message': 'Поле удалено'}

    except Exception as e:
        return {'success': False, 'error': str(e)}


def _get_next_client_kkt_slot(cursor, client_id):
    """Вернуть следующий свободный номер ККТ для клиента."""
    cursor.execute("""
        SELECT COALESCE(MAX(kkt_slot), 0) + 1
        FROM client_kkt
        WHERE client_id = %s
    """, [client_id])
    return int(cursor.fetchone()[0] or 1)


def _normalize_kkt_slot(value):
    """Привести номер ККТ к int или вернуть None для автонумерации."""
    if value in (None, ''):
        return None
    try:
        slot = int(value)
    except (TypeError, ValueError):
        raise ValueError('Номер ККТ должен быть положительным числом')
    if slot < 1:
        raise ValueError('Номер ККТ должен быть положительным числом')
    return slot


def _is_duplicate_kkt_slot_error(error):
    """Проверить, что IntegrityError вызван конфликтом уникального номера ККТ."""
    message = str(error)
    return 'Duplicate entry' in message and 'uq_client_kkt_client_slot' in message


def _insert_client_kkt(cursor, client_id, kkt_slot, data):
    cursor.execute("""
        INSERT INTO client_kkt (client_id, kkt_slot, address, name, phone, landmark,
            kkt_registered_at, kkt_model, kkt_serial_number, kkt_reg_number, kkt_firmware,
            fn_serial_number, fn_type, fn_manufacturer, ofd_inn, ofd_name,
            trade_system, sno, marking_enabled, marking_type,
            scanner_model, imei, sim_number, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
    """, [
        client_id, kkt_slot,
        data.get('address'), data.get('name'), data.get('phone'), data.get('landmark'),
        data.get('kkt_registered_at') or None, data.get('kkt_model'),
        data.get('kkt_serial_number'), data.get('kkt_reg_number'), data.get('kkt_firmware'),
        data.get('fn_serial_number'), data.get('fn_type'), data.get('fn_manufacturer'),
        data.get('ofd_inn'), data.get('ofd_name'), data.get('trade_system'), data.get('sno'),
        1 if data.get('marking_enabled') else 0, data.get('marking_type'),
        data.get('scanner_model'), data.get('imei'), data.get('sim_number')
    ])
    cursor.execute("SELECT LAST_INSERT_ID()")
    return cursor.fetchone()[0]


def add_client_kkt(user, client_id, data):
    """Добавить ККТ клиенту и вернуть API payload."""
    try:
        kkt_slot = _normalize_kkt_slot(data.get('kkt_slot'))

        with connection.cursor() as cursor:
            if kkt_slot is None:
                kkt_slot = _get_next_client_kkt_slot(cursor, client_id)
            else:
                cursor.execute(
                    "SELECT 1 FROM client_kkt WHERE client_id = %s AND kkt_slot = %s",
                    [client_id, kkt_slot]
                )
                if cursor.fetchone():
                    kkt_slot = _get_next_client_kkt_slot(cursor, client_id)

            try:
                new_id = _insert_client_kkt(cursor, client_id, kkt_slot, data)
            except IntegrityError as error:
                if not _is_duplicate_kkt_slot_error(error):
                    raise
                kkt_slot = _get_next_client_kkt_slot(cursor, client_id)
                new_id = _insert_client_kkt(cursor, client_id, kkt_slot, data)

        return {'success': True, 'message': 'ККТ добавлен', 'kkt_id': new_id, 'kkt_slot': kkt_slot}
    except ValueError as e:
        return {'success': False, 'error': str(e)}
    except Exception:
        logger.exception('Failed to add client KKT for client_id=%s', client_id)
        return {'success': False, 'error': 'Не удалось добавить ККТ. Попробуйте обновить страницу.'}


def delete_client_kkt(user, client_id, kkt_id):
    """Удалить ККТ клиента и вернуть API payload."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM client_kkt WHERE id = %s AND client_id = %s", [kkt_id, client_id])
        return {'success': True, 'message': 'ККТ удалён'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def update_client_kkt(user, client_id, kkt_id, data):
    """Обновить поле ККТ клиента и вернуть API payload."""
    try:
        field = data.get('field')
        value = data.get('value')

        # Разрешённые поля для редактирования
        allowed_fields = [
            'address', 'name', 'phone', 'landmark', 'kkt_registered_at',
            'kkt_model', 'kkt_serial_number', 'kkt_reg_number', 'kkt_firmware',
            'fn_serial_number', 'fn_type', 'fn_manufacturer',
            'ofd_inn', 'ofd_name', 'trade_system', 'sno',
            'marking_enabled', 'marking_type', 'scanner_model', 'imei', 'sim_number',
            # Честный Знак
            'chz_registered', 'chz_token', 'chz_has_ep', 'chz_product_type', 'chz_fias_id',
            # ТСПИоТ
            'tspiot_installed_at', 'tspiot_tariff_expires_at', 'tspiot_vendor',
            # Сервисы Прошивки
            'ecp_enabled', 'ecp_type', 'ecp_expires_at', 'to_kkt_yurt',
            'has_sim', 'sim_payment_at', 'ofd_expires_field', 'evotor_utm', 'ecp_comment',
            # Сроки
            'fn_expires_at', 'ofd_expires_at', 'mark_expires_at'
        ]

        # Boolean поля
        bool_fields = ['marking_enabled', 'chz_registered', 'chz_has_ep', 'ecp_enabled', 'has_sim']

        if field not in allowed_fields:
            return {'success': False, 'error': 'Поле не разрешено'}

        with connection.cursor() as cursor:
            if field in bool_fields:
                value = 1 if value in ['1', 'true', True, 1] else 0
            db_value = value if value else None
            cursor.execute(f"""
                UPDATE client_kkt SET {field} = %s, updated_at = NOW()
                WHERE id = %s AND client_id = %s
            """, [db_value, kkt_id, client_id])

        return {'success': True, 'message': 'Сохранено'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def refresh_alfabank_ip(user, client_id, data=None):
    """Обновить статус заявки ИП из Альфа-Банк API и вернуть API payload."""
    with connection.cursor() as cursor:
        try:
            cursor.execute("""
                SELECT id, alfabank_id, status, alfabank_state, created_at, updated_at
                FROM alfabank_ip_applications
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, [client_id])
            cols = [col[0] for col in cursor.description]
            row = cursor.fetchone()
        except Exception:
            return {'success': False, 'error': 'Таблица заявок не существует'}

    if not row:
        return {'success': False, 'error': 'Заявка не найдена'}

    application = dict(zip(cols, row))
    alfabank_id = application.get('alfabank_id')
    if not alfabank_id:
        return {'success': False, 'error': 'Нет ID заявки в Альфа-Банке'}

    try:
        import sys
        _sdk_src = Path(__file__).resolve().parent.parent.parent / "alfabank_partner_api" / "src"
        if str(_sdk_src) not in sys.path:
            sys.path.insert(0, str(_sdk_src))
        from alfabank_partner_api import AlfaBankClient, ClientConfig, DEMO_API_KEY
        from alfabank_partner_api.models.leads import LeadState

        api_key = os.environ.get("ALFABANK_API_KEY") or DEMO_API_KEY
        base_url = os.environ.get("ALFABANK_BASE_URL") or "https://partner.alfabank.ru/public-api/v2"
        cfg = ClientConfig(api_key=api_key, base_url=base_url)

        with AlfaBankClient(cfg) as client:
            lead = client.leads.get(alfabank_id)

        raw_state = lead.state.value if hasattr(lead.state, "value") else str(lead.state)
        if lead.state == LeadState.DECLINED:
            new_status = "declined"
        elif lead.state == LeadState.CLOSED:
            from alfabank_partner_api.enums import ProductCode, ProductState
            new_status = "declined"
            for product in (lead.product_info or []):
                raw_prod = product.product_code.value if hasattr(product.product_code, "value") else product.product_code
                if raw_prod == ProductCode.LP_RKO.value:
                    raw_prod_state = product.state.value if hasattr(product.state, "value") else product.state
                    if raw_prod_state == ProductState.COMPLETED.value:
                        new_status = "approved"
                    break
        else:
            new_status = "pending"

        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE alfabank_ip_applications
                SET status = %s, alfabank_state = %s, updated_at = NOW()
                WHERE id = %s
            """, [new_status, raw_state, application['id']])

        return {
            'success': True,
            'status': new_status,
            'alfabank_state': raw_state,
            'updated_at': datetime.now().strftime('%d.%m.%Y %H:%M'),
        }

    except Exception as exc:
        logger.exception("Alfa-Bank IP refresh error for client %s", client_id)
        return {'success': False, 'error': str(exc)}
