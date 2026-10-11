"""Order domain services.

Request-independent order helpers extracted from ``bot_app.views.orders``.
The view layer keeps HTTP parsing/rendering while this module owns manual deal
creation, update, and delete business operations.
"""

from datetime import datetime

from django.db import connection

from ..sql_cursor import with_sql_rewrite
from .task_service import _build_client_brief, _upsert_task

connection = with_sql_rewrite(connection)


def _ensure_orders_manual_columns():
    """Добавляет колонки для ручных сделок в orders и order_items если их нет."""
    with connection.cursor() as cursor:
        # orders: description, author_name
        cursor.execute("SHOW COLUMNS FROM orders LIKE 'description'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE orders ADD COLUMN description TEXT DEFAULT NULL AFTER payment_info")
        cursor.execute("SHOW COLUMNS FROM orders LIKE 'author_name'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE orders ADD COLUMN author_name VARCHAR(255) DEFAULT NULL AFTER description")
        # orders: company_id для сделок с компаниями
        cursor.execute("SHOW COLUMNS FROM orders LIKE 'company_id'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE orders ADD COLUMN company_id INT DEFAULT NULL AFTER user_id")
        # orders: specialist_id для сделок от специалистов
        cursor.execute("SHOW COLUMNS FROM orders LIKE 'specialist_id'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE orders ADD COLUMN specialist_id INT DEFAULT NULL AFTER company_id")
        # orders: внутренний комментарий/заметка по сделке
        cursor.execute("SHOW COLUMNS FROM orders LIKE 'admin_note'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE orders ADD COLUMN admin_note TEXT DEFAULT NULL AFTER description")
        # order_items: legacy_item_type, legacy_item_title
        cursor.execute("SHOW COLUMNS FROM order_items LIKE 'legacy_item_type'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE order_items ADD COLUMN legacy_item_type VARCHAR(255) DEFAULT NULL")
        cursor.execute("SHOW COLUMNS FROM order_items LIKE 'legacy_item_title'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE order_items ADD COLUMN legacy_item_title VARCHAR(255) DEFAULT NULL")


def create_manual_deal(user, data):
    """Создать ручную сделку и связанную task-запись.

    Возвращает словарь с тем же payload, который view отдаёт как JsonResponse.
    """
    try:
        _ensure_orders_manual_columns()
        client_id = data.get('client_id')
        company_id = data.get('company_id')
        product = data.get('product', '').strip()
        amount = data.get('amount')
        description = data.get('description', '').strip()
        # В самой сделке сохраняем автора-специалиста как сейчас.
        specialist_id = data.get('specialist_id') if user.get('is_director') else user['id']
        status = data.get('status', 'CONFIRMED')

        if not client_id and not company_id:
            return {'success': False, 'error': 'Выберите клиента или компанию'}
        if not product and not description:
            return {'success': False, 'error': 'Заполните продукт или описание'}

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
                INSERT INTO orders (user_id, company_id, specialist_id, amount, status, source, description, author_name, created_at, updated_at, paid_at)
                VALUES (%s, %s, %s, %s, %s, 'manual', %s, %s, %s, %s, %s)
            """, [client_id or None, company_id or None, specialist_id or None, amount_int, status, description or None, user['name'], now, now, paid_at])
            order_id = cursor.lastrowid

            # Сохраняем позицию заказа с legacy-полями для отображения товара
            if product:
                cursor.execute("""
                    INSERT INTO order_items (order_id, qty, price, created_at, legacy_item_type, legacy_item_title)
                    VALUES (%s, 1, %s, %s, 'manual', %s)
                """, [order_id, amount_int, now, product])

        client_brief = _build_client_brief(client_id)
        _upsert_task(
            title=f"Сделка #{order_id} — {client_brief.get('client_name') or (f'Компания #{company_id}' if company_id else f'Клиент #{client_id}')}",
            description=(description or product or f"Сделка #{order_id}"),
            source_type='deal',
            source_id=order_id,
            dedupe_key=f"deal:order:{order_id}",
            assignee_id=None,
            priority='high',
            meta={
                **client_brief,
                'company_id': company_id,
                'channel': 'manual',
                'author': user['name'],
                'specialist': user['name'],
                'amount': amount_int,
                'payment_status': status,
            },
        )

        return {'success': True, 'order_id': order_id, 'message': 'Сделка создана'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def create_order_client(user, data):
    """Создать клиента из модалки создания сделки.

    Возвращает словарь с тем же payload, который view отдаёт как JsonResponse.
    """
    try:
        from .company_schema import _auto_create_or_link_company, _ensure_added_by_columns

        _ensure_added_by_columns()
        first_name = data.get('first_name', '').strip()
        last_name = data.get('last_name', '').strip()
        phone = data.get('phone', '').strip()
        email = data.get('email', '').strip()
        inn = data.get('inn', '').strip()
        org_name = data.get('org_name', '').strip()
        city = data.get('city', '').strip()

        if not first_name and not org_name:
            return {'success': False, 'error': 'Укажите имя или организацию'}

        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO users (
                    first_name, last_name, phone, email, inn,
                    org_name, city,
                    we_added, consent_accepted, is_verified, added_by_specialist_id, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    1, 0, 0, %s, NOW(), NOW()
                )
            """, [
                first_name or None, last_name or None, phone or None,
                email or None, inn or None, org_name or None, city or None,
                user['id'],
            ])
            client_id = cursor.lastrowid

        # Авто-создание компании если указана организация
        if org_name:
            _auto_create_or_link_company(client_id)

        return {'success': True, 'client_id': client_id}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def search_order_clients_and_companies(query):
    """Найти клиентов и компании для выбора при создании ручной сделки."""
    q = query.strip()
    if len(q) < 2:
        return {'success': True, 'clients': [], 'companies': []}

    from .company_schema import _ensure_companies_tables

    _ensure_companies_tables()

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, first_name, last_name, phone, inn, org_name
            FROM users
            WHERE first_name LIKE %s OR last_name LIKE %s OR phone LIKE %s
                  OR inn LIKE %s OR org_name LIKE %s
            ORDER BY first_name
            LIMIT 15
        """, [f'%{q}%'] * 5)
        columns = [col[0] for col in cursor.description]
        clients = [dict(zip(columns, row)) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT id, name, inn, phone, email
            FROM companies
            WHERE name LIKE %s OR inn LIKE %s OR phone LIKE %s OR email LIKE %s
            ORDER BY name
            LIMIT 10
        """, [f'%{q}%'] * 4)
        comp_columns = [col[0] for col in cursor.description]
        companies = [dict(zip(comp_columns, row)) for row in cursor.fetchall()]

    return {'success': True, 'clients': clients, 'companies': companies}


def update_order(user, order_id, data):
    """Обновить сделку: сумма, статус, описание, комментарий, дата оплаты и (для директора) специалист."""
    try:
        if not user.get('is_director'):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM orders WHERE id = %s AND (specialist_id = %s OR (source = 'manual' AND author_name = %s))", [order_id, user['id'], user['name']])
                if not cursor.fetchone():
                    return {'success': False, 'error': 'Нет доступа к этой сделке'}

        amount = data.get('amount')
        new_status = data.get('new_status')  # 'paid' / 'advance' / 'unpaid' / 'completed' / 'cancelled'
        mark_paid = data.get('mark_paid')  # обратная совместимость
        description = data.get('description')
        internal_comment = data.get('internal_comment')
        paid_at_raw = data.get('paid_at')
        specialist_id = data.get('specialist_id')

        updates = []
        params = []

        if amount is not None:
            try:
                amount_int = int(float(amount))
            except (ValueError, TypeError):
                return {'success': False, 'error': 'Некорректная сумма'}
            if amount_int < 0:
                return {'success': False, 'error': 'Сумма не может быть меньше 0'}
            updates.append("amount = %s")
            params.append(amount_int)

        now = datetime.now()

        # Новый формат статусов: paid / advance / unpaid
        if new_status:
            if new_status == 'paid':
                updates.append("status = 'CONFIRMED'")
                updates.append("paid_at = COALESCE(paid_at, %s)")
                params.append(now)
            elif new_status == 'advance':
                updates.append("status = 'ADVANCE'")
                updates.append("paid_at = COALESCE(paid_at, %s)")
                params.append(now)
            elif new_status == 'unpaid':
                updates.append("status = 'INIT'")
                updates.append("paid_at = NULL")
            elif new_status == 'cancelled':
                updates.append("status = 'CANCELLED'")
                updates.append("paid_at = NULL")
            elif new_status == 'completed':
                updates.append("status = 'COMPLETED'")
        elif mark_paid is True:
            updates.append("status = 'CONFIRMED'")
            updates.append("paid_at = COALESCE(paid_at, %s)")
            params.append(now)
        elif mark_paid is False:
            updates.append("status = 'INIT'")
            updates.append("paid_at = NULL")

        if description is not None:
            description = str(description).strip()
            if len(description) > 5000:
                description = description[:5000]
            updates.append("description = %s")
            params.append(description or None)

        if internal_comment is not None:
            internal_comment = str(internal_comment).strip()
            if len(internal_comment) > 5000:
                internal_comment = internal_comment[:5000]
            updates.append("admin_note = %s")
            params.append(internal_comment or None)

        if paid_at_raw is not None:
            if str(paid_at_raw).strip():
                try:
                    paid_at_custom = datetime.fromisoformat(str(paid_at_raw).strip())
                except Exception:
                    return {'success': False, 'error': 'Некорректная дата оплаты'}
                updates.append("paid_at = %s")
                params.append(paid_at_custom)
            else:
                updates.append("paid_at = NULL")

        if user.get('is_director') and specialist_id is not None:
            if specialist_id in ("", 0, "0", None):
                updates.append("specialist_id = NULL")
            else:
                try:
                    specialist_id_int = int(specialist_id)
                except (TypeError, ValueError):
                    return {'success': False, 'error': 'Некорректный специалист'}
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM specialists WHERE id = %s LIMIT 1", [specialist_id_int])
                    if not cursor.fetchone():
                        return {'success': False, 'error': 'Специалист не найден'}
                updates.append("specialist_id = %s")
                params.append(specialist_id_int)

        if not updates:
            return {'success': False, 'error': 'Нет данных для обновления'}

        updates.append("updated_at = %s")
        params.append(now)
        params.append(order_id)

        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE orders SET {', '.join(updates)} WHERE id = %s",
                params
            )
            if cursor.rowcount == 0:
                return {'success': False, 'error': 'Сделка не найдена'}

            # Обновляем цену в order_items если меняется сумма
            if amount is not None:
                cursor.execute(
                    "UPDATE order_items SET price = %s WHERE order_id = %s",
                    [amount_int, order_id]
                )

        return {'success': True, 'message': 'Заказ обновлён'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def delete_order(user, order_id):
    """Удалить заказ и его позиции. Тех. специалист может удалить только свои сделки."""
    if not user.get('is_director'):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM orders WHERE id = %s AND (specialist_id = %s OR (source = 'manual' AND author_name = %s))", [order_id, user['id'], user['name']])
            if not cursor.fetchone():
                return {'success': False, 'error': 'Нет доступа к этой сделке'}
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM order_items WHERE order_id = %s", [order_id])
            cursor.execute("DELETE FROM orders WHERE id = %s", [order_id])
            if cursor.rowcount == 0:
                return {'success': False, 'error': 'Заказ не найден'}
        return {'success': True, 'message': 'Заказ удалён'}
    except Exception as e:
        return {'success': False, 'error': str(e)}
