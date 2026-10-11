"""Company domain services.

Request-independent company helpers extracted from ``bot_app.views.companies``.
The view layer keeps decorators, request parsing, access checks, and HTTP
response construction while this module owns company contact and KKT SQL
operations plus payload creation.
"""

import logging

from django.db import IntegrityError, connection

from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)
logger = logging.getLogger(__name__)


KKT_ALLOWED_UPDATE_FIELDS = [
    'address', 'name', 'phone', 'landmark', 'kkt_registered_at',
    'kkt_model', 'kkt_serial_number', 'kkt_reg_number', 'kkt_firmware',
    'fn_serial_number', 'fn_type', 'fn_manufacturer',
    'ofd_inn', 'ofd_name', 'trade_system', 'sno',
    'marking_enabled', 'marking_type', 'scanner_model', 'imei', 'sim_number',
    'chz_registered', 'chz_token', 'chz_has_ep', 'chz_product_type', 'chz_fias_id',
    'tspiot_installed_at', 'tspiot_tariff_expires_at', 'tspiot_vendor',
    'ecp_enabled', 'ecp_type', 'ecp_expires_at', 'to_kkt_yurt',
    'has_sim', 'sim_payment_at', 'ofd_expires_field', 'evotor_utm', 'ecp_comment',
    'fn_expires_at', 'ofd_expires_at', 'mark_expires_at'
]

KKT_BOOL_FIELDS = ['marking_enabled', 'chz_registered', 'chz_has_ep', 'ecp_enabled', 'has_sim']


COMPANY_EDIT_ALLOWED_FIELDS = [
    'name', 'inn', 'kpp', 'ogrn', 'address', 'phone', 'email', 'website', 'description', 'note'
]


def _normalize_inn(value):
    """Нормализует ИНН: оставляет только цифры."""
    if value is None:
        return ''
    return ''.join(ch for ch in str(value) if ch.isdigit()).strip()


def list_companies(search_query=None):
    """Загрузить список компаний для CRM с текущей логикой поиска и display-полями."""
    search = search_query or ''

    with connection.cursor() as cursor:
        if search:
            cursor.execute("""
                SELECT
                    c.id, c.name, c.inn, c.kpp, c.phone, c.email, c.address, c.created_at,
                    c.added_by_specialist_id, c.added_by_partner_id,
                    s.name as specialist_added_by_name,
                    p.name as partner_added_by_name,
                    (SELECT COUNT(*) FROM company_contacts WHERE company_id = c.id) as contacts_count,
                    (SELECT COUNT(*) FROM client_kkt WHERE company_id = c.id) as kkt_count
                FROM companies c
                LEFT JOIN specialists s ON s.id = c.added_by_specialist_id
                LEFT JOIN specialists p ON p.id = c.added_by_partner_id
                WHERE c.name LIKE %s OR c.inn LIKE %s OR c.phone LIKE %s OR c.email LIKE %s
                ORDER BY c.created_at DESC
                LIMIT 100
            """, [f'%{search}%'] * 4)
        else:
            cursor.execute("""
                SELECT
                    c.id, c.name, c.inn, c.kpp, c.phone, c.email, c.address, c.created_at,
                    c.added_by_specialist_id, c.added_by_partner_id,
                    s.name as specialist_added_by_name,
                    p.name as partner_added_by_name,
                    (SELECT COUNT(*) FROM company_contacts WHERE company_id = c.id) as contacts_count,
                    (SELECT COUNT(*) FROM client_kkt WHERE company_id = c.id) as kkt_count
                FROM companies c
                LEFT JOIN specialists s ON s.id = c.added_by_specialist_id
                LEFT JOIN specialists p ON p.id = c.added_by_partner_id
                ORDER BY c.created_at DESC
                LIMIT 100
            """)

        columns = ['id', 'name', 'inn', 'kpp', 'phone', 'email', 'address', 'created_at',
                   'added_by_specialist_id', 'added_by_partner_id', 'specialist_added_by_name', 'partner_added_by_name',
                   'contacts_count', 'kkt_count']
        companies = [dict(zip(columns, row)) for row in cursor.fetchall()]

    for company in companies:
        if company.get('added_by_partner_id') and company.get('partner_added_by_name'):
            company['added_by_name'] = f"Партнёр: {company['partner_added_by_name']}"
        else:
            company['added_by_name'] = company.get('specialist_added_by_name')

    return companies


def get_companies_list_context_data(user, filters=None):
    """Собрать request-independent context data для списка компаний CRM."""
    filters = filters or {}
    search = filters.get('search', '') or ''
    return {
        'companies': list_companies(search),
        'search': search,
        'active_menu': 'companies',
        'user': user,
    }


def _normalize_company_create_payload(data):
    """Подготовить payload создания компании так же, как это делал view."""
    return {
        'name': (data.get('name', '') or '').strip(),
        'inn': (data.get('inn', '') or '').strip(),
        'kpp': (data.get('kpp', '') or '').strip(),
        'ogrn': (data.get('ogrn', '') or '').strip(),
        'address': (data.get('address', '') or '').strip(),
        'phone': (data.get('phone', '') or '').strip(),
        'email': (data.get('email', '') or '').strip(),
        'website': (data.get('website', '') or '').strip(),
        'description': (data.get('description', '') or '').strip(),
        'note': (data.get('note', '') or '').strip(),
    }


def create_company_from_payload(user, data):
    """Создать компанию из request-independent payload формы CRM."""
    form_data = _normalize_company_create_payload(data)
    errors = {}

    if not form_data['name']:
        errors['name'] = 'Название компании обязательно'

    if errors:
        return {
            'success': False,
            'errors': errors,
            'form_data': form_data,
        }

    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO companies (name, inn, kpp, ogrn, address, phone, email, website, description, note, added_by_specialist_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, [
            form_data['name'],
            form_data['inn'] or None,
            form_data['kpp'] or None,
            form_data['ogrn'] or None,
            form_data['address'] or None,
            form_data['phone'] or None,
            form_data['email'] or None,
            form_data['website'] or None,
            form_data['description'] or None,
            form_data['note'] or None,
            user['id'],
        ])
        cursor.execute("SELECT LAST_INSERT_ID()")
        new_id = cursor.fetchone()[0]

    return {
        'success': True,
        'company_id': new_id,
        'message': 'Компания создана',
        'form_data': form_data,
    }


def get_company_by_id(company_id):
    """Загрузить данные компании для карточки CRM."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT c.id, c.name, c.inn, c.kpp, c.ogrn, c.address, c.phone, c.email, c.website, c.description,
                   COALESCE(c.note, c.description) as note,
                   c.created_at, c.updated_at, c.added_by_specialist_id, c.added_by_partner_id,
                   s.name as added_by_name, s.profession as added_by_profession,
                   p.name as partner_owner_name
            FROM companies c
            LEFT JOIN specialists s ON s.id = c.added_by_specialist_id
            LEFT JOIN specialists p ON p.id = c.added_by_partner_id
            WHERE c.id = %s
        """, [company_id])
        columns = [col[0] for col in cursor.description]
        row = cursor.fetchone()
        if not row:
            return None
        return dict(zip(columns, row))


def get_company_contacts(company_id):
    """Загрузить контакты компании для карточки CRM."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT cc.id as link_id, cc.role, cc.created_at as linked_at,
                   u.id as contact_id, u.first_name, u.last_name, u.phone, u.email,
                   u.tg_username, u.inn as contact_inn, u.org_name
            FROM company_contacts cc
            JOIN users u ON u.id = cc.contact_id
            WHERE cc.company_id = %s
            ORDER BY cc.created_at DESC
        """, [company_id])
        contact_columns = [col[0] for col in cursor.description]
        return [dict(zip(contact_columns, row)) for row in cursor.fetchall()]


def get_company_kkt(company_id):
    """Загрузить список ККТ компании для карточки CRM."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, kkt_slot, address, name, phone, landmark,
                   kkt_registered_at, kkt_model, kkt_serial_number, kkt_reg_number, kkt_firmware,
                   fn_serial_number, fn_type, fn_manufacturer,
                   ofd_inn, ofd_name, trade_system, sno,
                   marking_enabled, marking_type, scanner_model, imei, sim_number,
                   COALESCE(chz_registered, 0) as chz_registered, chz_token,
                   COALESCE(chz_has_ep, 0) as chz_has_ep, chz_product_type, chz_fias_id,
                   tspiot_installed_at, tspiot_tariff_expires_at, tspiot_vendor,
                   COALESCE(ecp_enabled, 0) as ecp_enabled, ecp_type, ecp_expires_at,
                   to_kkt_yurt, COALESCE(has_sim, 0) as has_sim, sim_payment_at,
                   ofd_expires_field, evotor_utm, ecp_comment,
                   fn_expires_at, ofd_expires_at, mark_expires_at,
                   created_at, updated_at
            FROM client_kkt WHERE company_id = %s ORDER BY kkt_slot
        """, [company_id])
        kkt_columns = [col[0] for col in cursor.description]
        return [dict(zip(kkt_columns, row)) for row in cursor.fetchall()]


def get_company_orders(company_id):
    """Загрузить связанные сделки компании для карточки CRM."""
    orders = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT o.id, o.user_id, o.amount, o.status, o.source,
                       o.description, o.admin_note AS internal_comment, o.author_name, o.created_at, o.paid_at,
                       o.specialist_id,
                       u.first_name, u.last_name,
                       sp.name AS specialist_name, sp.profession AS specialist_profession,
                       items.item_names
                FROM orders o
                LEFT JOIN users u ON u.id = o.user_id
                LEFT JOIN specialists sp ON sp.id = o.specialist_id
                LEFT JOIN (
                    SELECT oi.order_id,
                           GROUP_CONCAT(
                               DISTINCT COALESCE(oi.legacy_item_title, CONCAT('Позиция #', oi.id))
                               ORDER BY oi.id ASC SEPARATOR ', '
                           ) AS item_names
                    FROM order_items oi
                    GROUP BY oi.order_id
                ) items ON items.order_id = o.id
                WHERE o.company_id = %s
                ORDER BY o.created_at DESC
                LIMIT 50
            """, [company_id])
            order_columns = [col[0] for col in cursor.description]
            orders = [dict(zip(order_columns, row)) for row in cursor.fetchall()]
    except Exception:
        pass
    return orders


def get_company_detail_data(company_id, user=None, include_orders=True):
    """Собрать request-independent данные карточки компании."""
    company = get_company_by_id(company_id)
    if not company:
        return None

    detail_data = {
        'company': company,
        'contacts': get_company_contacts(company_id),
        'kkt_list': get_company_kkt(company_id),
    }
    if include_orders:
        detail_data['orders'] = get_company_orders(company_id)
    return detail_data


def update_company_from_payload(user, company_id, data):
    """Обновить разрешённое поле компании из API payload."""
    try:
        field = data.get('field')
        value = data.get('value')

        if field not in COMPANY_EDIT_ALLOWED_FIELDS:
            return {'success': False, 'error': 'Поле не разрешено для редактирования'}

        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE id = %s", [company_id])
            if not cursor.fetchone():
                return {'success': False, 'error': 'Компания не найдена'}

            if field == 'inn':
                normalized_inn = _normalize_inn(value)
                if normalized_inn:
                    cursor.execute("SELECT id FROM companies WHERE inn = %s AND id != %s LIMIT 1", [normalized_inn, company_id])
                    if cursor.fetchone():
                        return {'success': False, 'error': 'Извините, компания с таким ИНН уже есть в базе.'}
                value = normalized_inn or None

            cursor.execute(f"""
                UPDATE companies SET {field} = %s, updated_at = NOW() WHERE id = %s
            """, [value if value else None, company_id])

        return {'success': True, 'message': 'Данные обновлены'}

    except Exception as e:
        return {'success': False, 'error': str(e)}


def search_contacts_for_company(query):
    """Найти контакты для автокомплита при добавлении в компанию."""
    q = (query or '').strip()
    if len(q) < 2:
        return {'success': True, 'contacts': []}

    phone_digits = ''.join(ch for ch in q if ch.isdigit())
    phone_like_param = None
    if len(phone_digits) >= 3:
        # Нормализуем ввод для поиска телефона:
        # - 8XXXXXXXXXX -> 7XXXXXXXXXX
        # - 10 цифр мобильного -> добавляем 7
        if len(phone_digits) == 11 and phone_digits.startswith('8'):
            phone_digits = f"7{phone_digits[1:]}"
        elif len(phone_digits) == 10 and phone_digits.startswith('9'):
            phone_digits = f"7{phone_digits}"
        phone_like_param = f"%{phone_digits}%"

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, first_name, last_name, phone, email, tg_username
            FROM users
            WHERE first_name LIKE %s
               OR last_name LIKE %s
               OR phone LIKE %s
               OR email LIKE %s
               OR (
                   %s IS NOT NULL
                   AND REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),'.','') LIKE %s
               )
            ORDER BY first_name
            LIMIT 20
        """, [f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%', phone_like_param, phone_like_param])
        columns = [col[0] for col in cursor.description]
        contacts = [dict(zip(columns, row)) for row in cursor.fetchall()]

    return {'success': True, 'contacts': contacts}


def add_company_contact(user, company_id, data):
    """Привязать контакт к компании (из существующих или создать нового)."""
    try:
        contact_id = data.get('contact_id')
        role = data.get('role', '').strip()

        # Если contact_id не задан — создаём нового контакта
        if not contact_id:
            first_name = data.get('first_name', '').strip()
            last_name = data.get('last_name', '').strip()
            phone = data.get('phone', '').strip()
            email = data.get('email', '').strip()

            if not first_name:
                return {'success': False, 'error': 'Укажите имя контакта'}

            with connection.cursor() as cursor:
                # Получаем данные компании для заполнения org_name, inn, kpp
                cursor.execute("""
                    SELECT name, inn, kpp FROM companies WHERE id = %s
                """, [company_id])
                company_row = cursor.fetchone()
                comp_name = company_row[0] if company_row else None
                comp_inn = company_row[1] if company_row else None
                comp_kpp = company_row[2] if company_row else None

                cursor.execute("""
                    INSERT INTO users (first_name, last_name, phone, email, org_name, inn, kpp,
                                       we_added, consent_accepted, is_verified, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 0, 0, NOW(), NOW())
                """, [first_name, last_name or None, phone or None, email or None,
                      comp_name, comp_inn, comp_kpp])
                cursor.execute("SELECT LAST_INSERT_ID()")
                contact_id = cursor.fetchone()[0]

        with connection.cursor() as cursor:
            # Проверяем что контакт существует
            cursor.execute("SELECT id FROM users WHERE id = %s", [contact_id])
            if not cursor.fetchone():
                return {'success': False, 'error': 'Контакт не найден'}

            # Привязываем
            cursor.execute("""
                INSERT INTO company_contacts (company_id, contact_id, role)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE role = VALUES(role)
            """, [company_id, contact_id, role or None])

            # Для существующих контактов — обновляем org_name если пустое
            cursor.execute("""
                UPDATE users SET org_name = COALESCE(org_name, (SELECT name FROM companies WHERE id = %s))
                WHERE id = %s AND (org_name IS NULL OR org_name = '')
            """, [company_id, contact_id])

        return {'success': True, 'message': 'Контакт добавлен', 'contact_id': contact_id}

    except Exception as e:
        return {'success': False, 'error': str(e)}


def remove_company_contact(user, company_id, contact_id):
    """Отвязать контакт от компании."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                DELETE FROM company_contacts WHERE company_id = %s AND contact_id = %s
            """, [company_id, contact_id])

        return {'success': True, 'message': 'Контакт отвязан'}

    except Exception as e:
        return {'success': False, 'error': str(e)}


def _get_next_company_kkt_slot(cursor, company_id):
    """Вернуть следующий свободный номер ККТ для компании."""
    cursor.execute("""
        SELECT COALESCE(MAX(kkt_slot), 0) + 1
        FROM client_kkt
        WHERE company_id = %s
    """, [company_id])
    return int(cursor.fetchone()[0] or 1)


def _slot_is_busy_for_company_kkt(cursor, company_id, owner_contact_id, kkt_slot):
    """Проверить занятость номера в рамках компании и связанного контакта-владельца."""
    if owner_contact_id:
        cursor.execute("""
            SELECT 1
            FROM client_kkt
            WHERE kkt_slot = %s AND (company_id = %s OR client_id = %s)
            LIMIT 1
        """, [kkt_slot, company_id, owner_contact_id])
    else:
        cursor.execute("""
            SELECT 1
            FROM client_kkt
            WHERE company_id = %s AND kkt_slot = %s
            LIMIT 1
        """, [company_id, kkt_slot])
    return bool(cursor.fetchone())


def _get_available_company_kkt_slot(cursor, company_id, owner_contact_id, preferred_slot=None):
    """Вернуть свободный номер ККТ с учётом company_id и owner_contact_id."""
    kkt_slot = preferred_slot or _get_next_company_kkt_slot(cursor, company_id)
    while _slot_is_busy_for_company_kkt(cursor, company_id, owner_contact_id, kkt_slot):
        kkt_slot += 1
    return kkt_slot


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
    return 'Duplicate entry' in message and 'uq_client_kkt' in message


def _insert_company_kkt(cursor, company_id, owner_contact_id, kkt_slot, data):
    cursor.execute("""
        INSERT INTO client_kkt (client_id, company_id, kkt_slot, address, name, phone, landmark,
            kkt_registered_at, kkt_model, kkt_serial_number, kkt_reg_number, kkt_firmware,
            fn_serial_number, fn_type, fn_manufacturer, ofd_inn, ofd_name,
            trade_system, sno, marking_enabled, marking_type,
            scanner_model, imei, sim_number, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
    """, [
        owner_contact_id, company_id, kkt_slot,
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


def add_company_kkt(user, company_id, data):
    """Добавить ККТ в компанию."""
    try:
        kkt_slot = _normalize_kkt_slot(data.get('kkt_slot'))

        with connection.cursor() as cursor:
            owner_contact_id = data.get('owner_contact_id')
            if owner_contact_id:
                owner_contact_id = int(owner_contact_id)
                cursor.execute(
                    "SELECT 1 FROM company_contacts WHERE company_id=%s AND contact_id=%s",
                    [company_id, owner_contact_id]
                )
                if not cursor.fetchone():
                    owner_contact_id = None

            if kkt_slot is None:
                kkt_slot = _get_available_company_kkt_slot(cursor, company_id, owner_contact_id)
            else:
                kkt_slot = _get_available_company_kkt_slot(
                    cursor, company_id, owner_contact_id, preferred_slot=kkt_slot
                )

            try:
                new_id = _insert_company_kkt(cursor, company_id, owner_contact_id, kkt_slot, data)
            except IntegrityError as error:
                if not _is_duplicate_kkt_slot_error(error):
                    raise
                kkt_slot = _get_available_company_kkt_slot(cursor, company_id, owner_contact_id)
                new_id = _insert_company_kkt(cursor, company_id, owner_contact_id, kkt_slot, data)

        return {'success': True, 'message': 'ККТ добавлен', 'kkt_id': new_id, 'kkt_slot': kkt_slot}
    except ValueError as e:
        return {'success': False, 'error': str(e)}
    except Exception:
        logger.exception('Failed to add company KKT for company_id=%s', company_id)
        return {'success': False, 'error': 'Не удалось добавить ККТ. Попробуйте обновить страницу.'}


def delete_company_kkt(user, company_id, kkt_id):
    """Удалить ККТ компании."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM client_kkt WHERE id = %s AND company_id = %s", [kkt_id, company_id])
        return {'success': True, 'message': 'ККТ удалён'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def update_company_kkt(user, company_id, kkt_id, data):
    """Обновить поле ККТ компании."""
    try:
        field = data.get('field')
        value = data.get('value')

        if field not in KKT_ALLOWED_UPDATE_FIELDS:
            return {'success': False, 'error': 'Поле не разрешено'}

        with connection.cursor() as cursor:
            if field in KKT_BOOL_FIELDS:
                value = 1 if value in ['1', 'true', True, 1] else 0
            db_value = value if value else None
            cursor.execute(f"""
                UPDATE client_kkt SET {field} = %s, updated_at = NOW()
                WHERE id = %s AND company_id = %s
            """, [db_value, kkt_id, company_id])

        return {'success': True, 'message': 'Данные обновлены'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def delete_company(user, company_id):
    """Удалить компанию."""
    try:
        with connection.cursor() as cursor:
            # Проверяем что компания существует
            cursor.execute("SELECT id FROM companies WHERE id = %s", [company_id])
            if not cursor.fetchone():
                return {'success': False, 'error': 'Компания не найдена'}, 404

            # Удаляем компанию (каскадно удалит company_contacts)
            cursor.execute("DELETE FROM companies WHERE id = %s", [company_id])

        return {'success': True, 'message': 'Компания удалена'}, None
    except Exception as e:
        logger.exception('Failed to delete company id=%s', company_id)
        return {'success': False, 'error': str(e)}, None
