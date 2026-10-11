"""System settings and external products views."""

import json
import logging
from pathlib import Path

from django.db import connection
from django.shortcuts import redirect, render

from ..permissions import director_required, get_current_user, login_required_custom
from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_SETTINGS = [
    {
        'name': 'EXTERNAL_OFD_MARKUP_PCT',
        'value_type': 'number',
        'number_value': 740,
        'char_value': None,
        'description': 'Глобальная наценка для внешних товаров ОФД',
    },
    {
        'name': 'EXTERNAL_SERVICES_MARKUP_PCT',
        'value_type': 'number',
        'number_value': 740,
        'char_value': None,
        'description': 'Глобальная наценка для внешних сервисов',
    },
    {
        'name': 'EXTERNAL_EDO_MARKUP_PCT',
        'value_type': 'number',
        'number_value': 740,
        'char_value': None,
        'description': 'Глобальная наценка для внешних ЭДО',
    },
]


def _ensure_system_settings_table():
    """Создаёт таблицу системных настроек и базовые записи при первом обращении."""
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                name VARCHAR(100) PRIMARY KEY,
                value_type ENUM('number', 'text') NOT NULL,
                char_value VARCHAR(1000) DEFAULT NULL,
                number_value DECIMAL(12,4) DEFAULT NULL,
                description VARCHAR(255) DEFAULT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                updated_by BIGINT DEFAULT NULL,
                INDEX idx_system_settings_value_type (value_type),
                INDEX idx_system_settings_updated_by (updated_by),
                CONSTRAINT fk_system_settings_updated_by
                    FOREIGN KEY (updated_by) REFERENCES specialists(id)
                    ON DELETE SET NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        for item in DEFAULT_SYSTEM_SETTINGS:
            cursor.execute("""
                INSERT INTO system_settings (name, value_type, char_value, number_value, description)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    value_type = VALUES(value_type),
                    description = VALUES(description)
            """, [
                item['name'],
                item['value_type'],
                item['char_value'],
                item['number_value'],
                item['description'],
            ])


def _get_unread_notif_count_for_settings() -> int:
    from .notifications import _get_unread_notif_count

    return _get_unread_notif_count()


def _get_system_settings_rows():
    _ensure_system_settings_table()
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT ss.name, ss.value_type, ss.char_value, ss.number_value, ss.description,
                   ss.updated_at, s.name AS updated_by_name
            FROM system_settings ss
            LEFT JOIN specialists s ON s.id = ss.updated_by
            ORDER BY ss.name ASC
        """)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    for row in rows:
        if row.get('number_value') is not None:
            row['number_value_display'] = str(row['number_value']).rstrip('0').rstrip('.')
        else:
            row['number_value_display'] = ''
    return rows


@login_required_custom
@director_required
def system_settings_page(request):
    """Настройки системы для директора."""
    user = get_current_user(request)
    error = None
    success = request.GET.get('saved') == '1'

    _ensure_system_settings_table()

    if request.method == 'POST':
        setting_name = request.POST.get('setting_name', '').strip()
        value_type = request.POST.get('value_type', '').strip()
        char_value = request.POST.get('char_value', '').strip()
        number_raw = request.POST.get('number_value', '').strip()

        if not setting_name:
            error = 'Не указан ключ настройки.'
        elif value_type not in {'number', 'text'}:
            error = 'Неверный тип настройки.'
        else:
            try:
                with connection.cursor() as cursor:
                    if value_type == 'number':
                        if not number_raw:
                            raise ValueError('Укажите числовое значение.')
                        try:
                            number_value = float(number_raw.replace(',', '.'))
                        except ValueError as exc:
                            raise ValueError('Введите корректное число.') from exc
                        cursor.execute("""
                            UPDATE system_settings
                            SET number_value = %s, char_value = NULL, updated_by = %s
                            WHERE name = %s AND value_type = 'number'
                        """, [number_value, user['id'], setting_name])
                    else:
                        if not char_value:
                            raise ValueError('Текстовое значение не должно быть пустым.')
                        cursor.execute("""
                            UPDATE system_settings
                            SET char_value = %s, number_value = NULL, updated_by = %s
                            WHERE name = %s AND value_type = 'text'
                        """, [char_value, user['id'], setting_name])

                    if cursor.rowcount == 0:
                        raise ValueError('Настройка не найдена или тип значения не совпадает.')
            except ValueError as exc:
                error = str(exc)
            except Exception:
                logger.exception('Failed to save system setting %s', setting_name)
                error = 'Не удалось сохранить настройку. Проверьте логи сервера.'
            else:
                return redirect(f"{request.path}?saved=1")

    settings_rows = _get_system_settings_rows()
    return render(request, 'bot_app/system_settings.html', {
        'user': user,
        'settings_rows': settings_rows,
        'success': success,
        'error': error,
        'unread_notif_count': _get_unread_notif_count_for_settings(),
    })




def _load_external_catalog_seed():
    base = Path(__file__).resolve().parent.parent.parent / 'bot'
    with open(base / 'operators.json', 'r', encoding='utf-8') as f:
        operators = json.load(f)
    with open(base / 'ofd_deadlines.json', 'r', encoding='utf-8') as f:
        deadlines = json.load(f)
    return operators, deadlines


def _ensure_external_catalog_tables():
    """Создаёт таблицы внешнего каталога и переносит легаси-данные из ext_price_overrides."""
    operators, deadlines = _load_external_catalog_seed()

    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ext_product_operators (
                id INT AUTO_INCREMENT PRIMARY KEY,
                category ENUM('ofd', 'service', 'services_for_orders', 'edo') NOT NULL,
                operator_id INT NOT NULL,
                name VARCHAR(255) NOT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                sort_order INT NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_ext_product_operator (category, operator_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ext_product_types (
                id INT AUTO_INCREMENT PRIMARY KEY,
                type_id INT NOT NULL,
                name VARCHAR(255) NOT NULL,
                months_number INT DEFAULT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                sort_order INT NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_ext_product_type (type_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ext_products (
                id INT AUTO_INCREMENT PRIMARY KEY,
                category ENUM('ofd', 'service', 'services_for_orders', 'edo') NOT NULL,
                operator_id INT NOT NULL,
                type_id INT NOT NULL,
                external_name VARCHAR(255) DEFAULT NULL,
                source_price INT DEFAULT NULL,
                price_fixed INT DEFAULT NULL,
                markup_pct DECIMAL(8,2) DEFAULT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                comment VARCHAR(255) DEFAULT NULL,
                last_seen_at DATETIME DEFAULT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_ext_product (category, operator_id, type_id),
                KEY idx_ext_products_active (category, is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        category_map = {
            'ofd_operators': 'ofd',
            'services': 'service',
            'services_for_orders': 'services_for_orders',
            'edo': 'edo',
        }
        for source_key, category in category_map.items():
            for sort_order, (operator_id, name) in enumerate((operators.get(source_key) or {}).items(), start=1):
                cursor.execute("""
                    INSERT INTO ext_product_operators (category, operator_id, name, sort_order)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        sort_order = VALUES(sort_order)
                """, [category, int(operator_id), str(name).strip(), sort_order])

        for sort_order, (type_id, info) in enumerate((deadlines or {}).items(), start=1):
            cursor.execute("""
                INSERT INTO ext_product_types (type_id, name, months_number, sort_order)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    months_number = VALUES(months_number),
                    sort_order = VALUES(sort_order)
            """, [int(type_id), str((info or {}).get('name') or type_id).strip(), (info or {}).get('months_number'), sort_order])

        cursor.execute("""
            INSERT INTO ext_products (category, operator_id, type_id, price_fixed, markup_pct, is_active, comment, updated_at)
            SELECT e.category, e.operator_id, e.type_id, e.price_fixed, e.markup_pct, e.is_active, e.comment, e.updated_at
            FROM ext_price_overrides e
            LEFT JOIN ext_products p
              ON p.category = e.category AND p.operator_id = e.operator_id AND p.type_id = e.type_id
            WHERE p.id IS NULL
        """)


def _refresh_external_products_catalog():
    """Пробует подтянуть актуальные внешние продукты из API и синхронизировать их в БД."""
    try:
        from bot.extern_ofd import get_products
        for category in ('ofd', 'service', 'services_for_orders', 'edo'):
            try:
                get_products(category)
            except Exception:
                logger.exception('Failed to refresh external catalog for category=%s', category)
    except Exception:
        logger.exception('Failed to import external catalog refresher')


def _get_external_products_rows(category_filter: str = ''):
    _ensure_external_catalog_tables()
    params = []
    where = ''
    if category_filter:
        where = 'WHERE ep.category = %s'
        params.append(category_filter)

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT
                ep.id, ep.category, ep.operator_id, ep.type_id, ep.external_name, ep.source_price,
                ep.price_fixed, ep.markup_pct, ep.is_active, ep.comment, ep.last_seen_at, ep.updated_at,
                eo.name AS operator_name,
                et.name AS type_name,
                et.months_number
            FROM ext_products ep
            LEFT JOIN ext_product_operators eo
              ON eo.category = ep.category AND eo.operator_id = ep.operator_id
            LEFT JOIN ext_product_types et
              ON et.type_id = ep.type_id
            {where}
            ORDER BY FIELD(ep.category, 'ofd', 'service', 'services_for_orders', 'edo'), eo.sort_order, eo.name, et.sort_order, et.name, ep.id
        """, params)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    category_labels = {
        'ofd': 'ОФД',
        'service': 'Сервисы',
        'services_for_orders': 'Сервисы по заказу',
        'edo': 'ЭДО',
    }
    for row in rows:
        row['category_label'] = category_labels.get(row.get('category'), row.get('category'))
        row['display_name'] = row.get('operator_name') or row.get('external_name') or f"ID {row.get('operator_id')}"
        row['type_display'] = row.get('type_name') or '—'
        row['source_price_display'] = f"{row.get('source_price')} ₽" if row.get('source_price') is not None else '—'
        row['price_fixed_display'] = '' if row.get('price_fixed') is None else str(row.get('price_fixed'))
        row['markup_pct_display'] = '' if row.get('markup_pct') is None else str(row.get('markup_pct')).rstrip('0').rstrip('.')
    return rows


def _get_shop_products_rows():
    """Данные вкладки «Наш магазин»: оборудование + цены кодов ОФД."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, name, category, is_used, price, image_name, count
            FROM equipment
            ORDER BY category, name, id
        """)
        equipment_cols = [col[0] for col in cursor.description]
        equipment_rows = [dict(zip(equipment_cols, row)) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT
                code_type,
                COALESCE(MAX(sell_price), 0) AS sell_price,
                COUNT(*) AS codes_count
            FROM codes
            WHERE code_type IN ('ofd_platform', 'ofd_first')
            GROUP BY code_type
            ORDER BY FIELD(code_type, 'ofd_platform', 'ofd_first')
        """)
        code_cols = [col[0] for col in cursor.description]
        code_rows = [dict(zip(code_cols, row)) for row in cursor.fetchall()]

    code_labels = {
        'ofd_platform': 'ОФД Платформа',
        'ofd_first': 'ОФД Первый',
    }
    for row in code_rows:
        row['type_label'] = code_labels.get(row.get('code_type'), row.get('code_type'))
        row['sell_price_display'] = '' if row.get('sell_price') is None else str(int(row.get('sell_price')))

    return equipment_rows, code_rows


@login_required_custom
@director_required
def external_products_page(request):
    """Управление внешними товарами и правилами ценообразования."""
    user = get_current_user(request)
    error = None
    success = request.GET.get('saved') == '1'
    refreshed = request.GET.get('refreshed') == '1'
    active_tab = request.GET.get('tab', 'vseofd').strip().lower()
    if active_tab not in ('vseofd', 'shop'):
        active_tab = 'vseofd'
    category_filter = request.GET.get('category', '').strip()

    _ensure_external_catalog_tables()

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'save_shop_equipment':
            equipment_id_raw = request.POST.get('equipment_id', '').strip()
            price_raw = request.POST.get('price', '').strip()
            try:
                equipment_id = int(equipment_id_raw)
                price_value = int(price_raw)
                if price_value < 0:
                    raise ValueError
            except (TypeError, ValueError):
                error = 'Цена оборудования должна быть целым неотрицательным числом.'
            else:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("UPDATE equipment SET price = %s WHERE id = %s", [price_value, equipment_id])
                        if cursor.rowcount == 0:
                            error = 'Оборудование не найдено.'
                        else:
                            return redirect(f'{request.path}?tab=shop&saved=1')
                except Exception:
                    logger.exception('Failed to save shop equipment price id=%s', equipment_id)
                    error = 'Не удалось сохранить цену оборудования.'

        if action == 'delete_shop_equipment':
            equipment_id_raw = request.POST.get('equipment_id', '').strip()
            try:
                equipment_id = int(equipment_id_raw)
            except (TypeError, ValueError):
                error = 'Некорректный идентификатор оборудования.'
            else:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("DELETE FROM equipment WHERE id = %s", [equipment_id])
                        if cursor.rowcount == 0:
                            error = 'Оборудование не найдено.'
                        else:
                            return redirect(f'{request.path}?tab=shop&saved=1')
                except Exception:
                    logger.exception('Failed to delete shop equipment id=%s', equipment_id)
                    error = 'Не удалось удалить оборудование.'

        if action == 'add_shop_equipment':
            name = (request.POST.get('name') or '').strip()
            category = (request.POST.get('category') or '').strip()
            image_name = (request.POST.get('image_name') or '').strip() or None
            is_used = 1 if request.POST.get('is_used') else 0
            price_raw = request.POST.get('price', '').strip()
            count_raw = request.POST.get('count', '').strip()
            allowed_categories = {'Кассы', 'Сканер', 'ФН', 'Кассовые ленты', 'Этикетки'}

            if not name:
                error = 'Укажите название оборудования.'
            elif category not in allowed_categories:
                error = 'Выберите корректную категорию оборудования.'
            else:
                try:
                    price_value = int(price_raw)
                    if price_value < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    error = 'Цена оборудования должна быть целым неотрицательным числом.'
                    price_value = 0

                count_value = None
                if error is None and count_raw:
                    try:
                        count_value = int(count_raw)
                        if count_value < 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        error = 'Количество должно быть целым неотрицательным числом.'

            if error is None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO equipment (name, category, is_used, price, image_name, `count`)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            [name, category, is_used, price_value, image_name, count_value],
                        )
                    return redirect(f'{request.path}?tab=shop&saved=1')
                except Exception:
                    logger.exception('Failed to add new shop equipment name=%s category=%s', name, category)
                    error = 'Не удалось добавить оборудование.'

        if action == 'save_shop_code_price':
            code_type = (request.POST.get('code_type') or '').strip()
            price_raw = request.POST.get('sell_price', '').strip()
            if code_type not in ('ofd_platform', 'ofd_first'):
                error = 'Некорректный тип кода.'
            else:
                try:
                    sell_price = int(price_raw)
                    if sell_price < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    error = 'Цена кода должна быть целым неотрицательным числом.'
                else:
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "UPDATE codes SET sell_price = %s WHERE code_type = %s",
                                [sell_price, code_type],
                            )
                        return redirect(f'{request.path}?tab=shop&saved=1')
                    except Exception:
                        logger.exception('Failed to save shop code price code_type=%s', code_type)
                        error = 'Не удалось сохранить цену кода.'

        if action == 'refresh_catalog':
            _refresh_external_products_catalog()
            suffix = f'?tab={active_tab}&refreshed=1'
            if category_filter:
                suffix += f'&category={category_filter}'
            return redirect(f'{request.path}{suffix}')

        if action != 'save':
            product_id = ''
        else:
            product_id = request.POST.get('product_id', '').strip()
        try:
            product_id_int = int(product_id)
        except (TypeError, ValueError):
            if action == 'save':
                error = 'Некорректный идентификатор товара.'
        else:
            price_fixed_raw = request.POST.get('price_fixed', '').strip()
            markup_pct_raw = request.POST.get('markup_pct', '').strip()
            comment = request.POST.get('comment', '').strip() or None
            is_active = 1 if request.POST.get('is_active') else 0

            try:
                price_fixed = int(price_fixed_raw) if price_fixed_raw else None
            except ValueError:
                error = 'Фиксированная цена должна быть целым числом.'
                price_fixed = None

            if error is None:
                try:
                    markup_pct = round(float(markup_pct_raw.replace(',', '.')), 2) if markup_pct_raw else None
                except ValueError:
                    error = 'Наценка должна быть числом.'
                    markup_pct = None

            if error is None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            UPDATE ext_products
                            SET price_fixed = %s,
                                markup_pct = %s,
                                comment = %s,
                                is_active = %s
                            WHERE id = %s
                        """, [price_fixed, markup_pct, comment, is_active, product_id_int])
                        if cursor.rowcount == 0:
                            error = 'Товар не найден.'
                        else:
                            suffix = '?saved=1'
                            if active_tab:
                                suffix += f'&tab={active_tab}'
                            if category_filter:
                                suffix += f'&category={category_filter}'
                            return redirect(f'{request.path}{suffix}')
                except Exception:
                    logger.exception('Failed to save ext product id=%s', product_id_int)
                    error = 'Не удалось сохранить товар.'

    rows = _get_external_products_rows(category_filter=category_filter)
    equipment_rows, shop_code_rows = _get_shop_products_rows()
    if not rows:
        _refresh_external_products_catalog()
        rows = _get_external_products_rows(category_filter=category_filter)

    return render(request, 'bot_app/external_products.html', {
        'user': user,
        'rows': rows,
        'success': success,
        'refreshed': refreshed,
        'active_tab': active_tab,
        'error': error,
        'category_filter': category_filter,
        'equipment_rows': equipment_rows,
        'shop_code_rows': shop_code_rows,
        'category_choices': [
            ('', 'Все категории'),
            ('ofd', 'ОФД'),
            ('service', 'Сервисы'),
            ('services_for_orders', 'Сервисы по заказу'),
            ('edo', 'ЭДО'),
        ],
        'unread_notif_count': _get_unread_notif_count_for_settings(),
    })
