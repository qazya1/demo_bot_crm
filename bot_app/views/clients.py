"""CRM client views extracted from the legacy views module."""

import json
import logging
from datetime import date, datetime

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from ..permissions import (
    get_current_user,
    login_required_custom,
    director_required,
    tech_or_director_required,
    _check_specialist_client_access,
)
from ..sql_cursor import with_sql_rewrite
from .common import _normalize_inn
from .company_schema import (
    _auto_create_or_link_company,
    _ensure_added_by_columns,
    _ensure_users_birth_date_column,
)
from .dashboard_helpers import _get_all_specialists
from .orders import _ensure_address_columns
from ..services.client_service import (
    build_client_display as _build_client_display,
    _ensure_client_notes_table,
    add_client_kkt,
    delete_client_kkt,
    delete_client_note,
    delete_custom_field,
    get_client_notes,
    refresh_alfabank_ip,
    save_client_note,
    save_custom_field,
    search_clients,
    update_client_kkt,
)

connection = with_sql_rewrite(connection)
logger = logging.getLogger(__name__)
TASK_CLIENT_ACCESS_STATUSES = (
    'awaiting_assignment',
    'in_progress',
    'postponed',
    'done',
)




@login_required_custom
def api_clients_search(request):
    """API: поиск клиентов по имени, телефону, ИНН для выбора в задачах."""
    query = request.GET.get('q')
    return JsonResponse(search_clients(query))

@ensure_csrf_cookie

@login_required_custom
@director_required
def crm_contacts_list(request):
    """Список контактов (все пользователи)"""
    search = request.GET.get('search', '')

    with connection.cursor() as cursor:
        if search:
            cursor.execute("""
                SELECT
                    u.id,
                    u.tg_user_id,
                    u.first_name,
                    u.last_name,
                    u.phone,
                    u.email,
                    u.tg_username,
                    u.inn,
                    u.org_name,
                    u.city,
                    u.is_verified,
                    u.created_at
                FROM users u
                WHERE u.first_name LIKE %s OR u.last_name LIKE %s OR u.phone LIKE %s
                      OR u.inn LIKE %s OR u.org_name LIKE %s OR u.tg_username LIKE %s
                ORDER BY u.created_at DESC
                LIMIT 100
            """, [f'%{search}%'] * 6)
        else:
            cursor.execute("""
                SELECT
                    u.id,
                    u.tg_user_id,
                    u.first_name,
                    u.last_name,
                    u.phone,
                    u.email,
                    u.tg_username,
                    u.inn,
                    u.org_name,
                    u.city,
                    u.is_verified,
                    u.created_at
                FROM users u
                ORDER BY u.created_at DESC
                LIMIT 100
            """)

        columns = ['id', 'tg_user_id', 'first_name', 'last_name', 'phone', 'email', 'tg_username',
                   'inn', 'org_name', 'city', 'is_verified', 'created_at']
        contacts = [dict(zip(columns, row)) for row in cursor.fetchall()]

    return render(request, 'bot_app/crm/contacts_list.html', {
        'contacts': contacts,
        'search': search,
        'active_menu': 'contacts'
    })



@login_required_custom
@tech_or_director_required
def crm_clients_list(request):
    """Список клиентов. Директор видит всех, специалист — только разрешённых по доступам."""
    user = get_current_user(request)
    _ensure_added_by_columns()

    # Специалист без CRM-доступа видит только своих клиентов
    is_full_access = user.get('has_crm_access')

    search = request.GET.get('search', '')
    source_filter = request.GET.get('source', '')
    PER_PAGE = 50

    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    offset = (page - 1) * PER_PAGE

    with connection.cursor() as cursor:
        # Базовый фильтр: специалист видит клиентов из активных чатов,
        # созданных им вручную и прикреплённых к его задачам.
        # Для задач учитываем как task_clients, так и legacy-связи в tasks/meta_json.
        specialist_join = ""
        specialist_where = ""
        specialist_params = []
        if not is_full_access:
            spec_id = int(user['id'])
            task_statuses_sql = ", ".join(["%s"] * len(TASK_CLIENT_ACCESS_STATUSES))
            specialist_where = f"""
                AND (
                    EXISTS (
                        SELECT 1
                        FROM support_chats sc_access
                        WHERE sc_access.client_user_id = u.id
                          AND sc_access.specialist_id = %s
                          AND sc_access.status IN ('pending', 'active', 'consultation')
                    )
                    OR u.added_by_specialist_id = %s
                    OR EXISTS (
                        SELECT 1
                        FROM tasks t_access
                        WHERE t_access.assignee_id = %s
                          AND t_access.status IN ({task_statuses_sql})
                          AND (
                                EXISTS (
                                    SELECT 1
                                    FROM task_clients tc_access
                                    WHERE tc_access.task_id = t_access.id
                                      AND tc_access.client_id = u.id
                                )
                                OR t_access.client_id = u.id
                                OR (
                                    t_access.meta_json IS NOT NULL
                                    AND JSON_VALID(t_access.meta_json)
                                    AND (
                                        JSON_SEARCH(t_access.meta_json, 'one', CAST(u.id AS CHAR), NULL, '$.linked_client_ids[*]') IS NOT NULL
                                        OR JSON_SEARCH(t_access.meta_json, 'one', CAST(u.id AS CHAR), NULL, '$.linked_clients[*].id') IS NOT NULL
                                        OR JSON_UNQUOTE(JSON_EXTRACT(t_access.meta_json, '$.client_id')) = CAST(u.id AS CHAR)
                                    )
                                )
                          )
                    )
                )
            """
            specialist_params = [spec_id, spec_id, spec_id, *TASK_CLIENT_ACCESS_STATUSES]

        # Счётчики: для специалиста — только его клиенты
        if is_full_access:
            cursor.execute("SELECT COUNT(*) FROM users")
            total_clients_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM users WHERE tg_user_id IS NOT NULL OR max_user_id IS NOT NULL")
            activated_clients_count = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COUNT(*)
                FROM users
                WHERE inn IS NOT NULL AND TRIM(inn) != ''
                  AND phone IS NOT NULL AND TRIM(phone) != ''
            """)
            verified_clients_count = cursor.fetchone()[0]
        else:
            cursor.execute(f"""
                SELECT COUNT(DISTINCT u.id) FROM users u
                {specialist_join}
                WHERE 1=1 {specialist_where}
            """, specialist_params)
            total_clients_count = cursor.fetchone()[0]
            activated_clients_count = total_clients_count
            verified_clients_count = 0

        select_fields = """
                    u.id,
                    u.tg_user_id,
                    u.max_user_id,
                    u.inn,
                    u.org_name,
                    u.first_name,
                    u.last_name,
                    u.phone,
                    u.email,
                    u.tg_username,
                    u.city,
                    u.is_verified,
                    u.created_at,
                    u.added_by_specialist_id,
                    s.name as added_by_name,
                    u.added_by_partner_id,
                    u.source_partner_id,
                    p1.name as added_partner_name,
                    p2.name as source_partner_name,
                    u.partner_source_type,
                    (SELECT COUNT(*) FROM client_kkt WHERE client_id = u.id) as kkt_count,
                    COALESCE(lb.available_amount, 0) as loyalty_balance,
                    COALESCE(ulp.tier, '') as loyalty_tier"""

        base_joins = """
                LEFT JOIN specialists s ON s.id = u.added_by_specialist_id
                LEFT JOIN specialists p1 ON p1.id = u.added_by_partner_id
                LEFT JOIN specialists p2 ON p2.id = u.source_partner_id
                LEFT JOIN loyalty_accounts la ON la.owner_type = 'client' AND la.owner_id = u.id
                LEFT JOIN loyalty_balances lb ON lb.account_id = la.id
                LEFT JOIN user_loyalty_profile ulp ON ulp.user_id = u.id"""

        # При specialist-фильтрации используем EXISTS, поэтому GROUP BY не требуется.
        group_by = ""
        extra_join = specialist_join if not is_full_access else ""
        extra_where = specialist_where if not is_full_access else ""
        extra_params_prefix = specialist_params if not is_full_access else []

        if search:
            source_cond = ""
            search_params = [f'%{search}%'] * 5
            if source_filter == 'telegram':
                source_cond = "AND u.tg_user_id IS NOT NULL"
            elif source_filter == 'max':
                source_cond = "AND u.max_user_id IS NOT NULL"

            cursor.execute(f"""
                SELECT COUNT(DISTINCT u.id) FROM users u
                {extra_join}
                WHERE (u.inn LIKE %s OR u.org_name LIKE %s OR u.phone LIKE %s
                       OR u.first_name LIKE %s OR u.last_name LIKE %s)
                {source_cond}
                {extra_where}
            """, extra_params_prefix + list(search_params))
            filtered_count = cursor.fetchone()[0]

            cursor.execute(f"""
                SELECT {select_fields}
                FROM users u
                {extra_join}
                {base_joins}
                WHERE (u.inn LIKE %s OR u.org_name LIKE %s OR u.phone LIKE %s
                       OR u.first_name LIKE %s OR u.last_name LIKE %s)
                {source_cond}
                {extra_where}
                {group_by}
                ORDER BY u.created_at DESC
                LIMIT %s OFFSET %s
            """, extra_params_prefix + search_params + [PER_PAGE, offset])
        else:
            if source_filter == 'telegram':
                where_clause = "WHERE u.tg_user_id IS NOT NULL"
            elif source_filter == 'max':
                where_clause = "WHERE u.max_user_id IS NOT NULL"
            else:
                where_clause = ""

            # Для specialist фильтрации: добавляем specialist_where
            if not is_full_access:
                if where_clause:
                    where_clause += f" {extra_where}"
                else:
                    where_clause = f"WHERE 1=1 {extra_where}"

            cursor.execute(f"""
                SELECT COUNT(DISTINCT u.id) FROM users u
                {extra_join}
                {where_clause}
            """, extra_params_prefix)
            filtered_count = cursor.fetchone()[0]

            cursor.execute(f"""
                SELECT {select_fields}
                FROM users u
                {extra_join}
                {base_joins}
                {where_clause}
                {group_by}
                ORDER BY u.created_at DESC
                LIMIT %s OFFSET %s
            """, extra_params_prefix + [PER_PAGE, offset])

        columns = ['id', 'tg_user_id', 'max_user_id', 'inn', 'org_name', 'first_name', 'last_name',
                   'phone', 'email', 'tg_username', 'city', 'is_verified', 'created_at', 'added_by_specialist_id', 'added_by_name',
                   'added_by_partner_id', 'source_partner_id', 'added_partner_name', 'source_partner_name', 'partner_source_type', 'kkt_count',
                   'loyalty_balance', 'loyalty_tier']
        clients = [dict(zip(columns, row)) for row in cursor.fetchall()]
        for c in clients:
            if c.get('added_partner_name') or c.get('source_partner_name'):
                c['added_by_display'] = f"Партнёр: {c.get('added_partner_name') or c.get('source_partner_name')}"
            elif c.get('added_by_name'):
                c['added_by_display'] = c['added_by_name']
            else:
                c['added_by_display'] = '—'

    total_pages = max(1, (filtered_count + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)

    # Диапазон страниц вокруг текущей (±2)
    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)
    page_range = list(range(start_page, end_page + 1))

    return render(request, 'bot_app/crm/clients_list.html', {
        'clients': clients,
        'shown_clients_count': len(clients),
        'total_clients_count': total_clients_count,
        'filtered_count': filtered_count,
        'activated_clients_count': activated_clients_count,
        'verified_clients_count': verified_clients_count,
        'search': search,
        'source_filter': source_filter,
        'active_menu': 'clients',
        'user': user,
        'page': page,
        'total_pages': total_pages,
        'per_page': PER_PAGE,
        'page_range': page_range,
    })

@login_required_custom
@tech_or_director_required
def crm_client_create(request):
    """Создание нового клиента через интерфейс CRM (директор и тех. специалисты)"""
    _ensure_added_by_columns()
    user = get_current_user(request)
    errors = {}
    form_data = {}

    if request.method == 'POST':
        form_data = {
            'first_name': request.POST.get('first_name', '').strip(),
            'last_name': request.POST.get('last_name', '').strip(),
            'phone': request.POST.get('phone', '').strip(),
            'email': request.POST.get('email', '').strip(),
            'inn': request.POST.get('inn', '').strip(),
            'org_name': request.POST.get('org_name', '').strip(),
            'kpp': request.POST.get('kpp', '').strip(),
            'city': request.POST.get('city', '').strip(),
            'tg_username': request.POST.get('tg_username', '').strip(),
        }
        normalized_inn = _normalize_inn(form_data.get('inn'))
        form_data['inn'] = normalized_inn

        # Валидация: хотя бы имя или организация должны быть заполнены
        if not form_data['first_name'] and not form_data['org_name']:
            errors['first_name'] = 'Укажите имя или название компании'

        if not errors:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO users (
                        first_name, last_name, phone, email, inn,
                        org_name, kpp, city, tg_username,
                        we_added, consent_accepted, is_verified, added_by_specialist_id, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, 0, 0, %s, NOW(), NOW()
                    )
                """, [
                    form_data['first_name'] or None,
                    form_data['last_name'] or None,
                    form_data['phone'] or None,
                    form_data['email'] or None,
                    form_data['inn'] or None,
                    form_data['org_name'] or None,
                    form_data['kpp'] or None,
                    form_data['city'] or None,
                    form_data['tg_username'] or None,
                    1,
                    user['id'],
                ])
                cursor.execute("SELECT LAST_INSERT_ID()")
                new_id = cursor.fetchone()[0]

            # Авто-создание компании если указана организация
            if form_data.get('org_name'):
                _auto_create_or_link_company(new_id)

            return redirect('bot_app:crm_client_detail', client_id=new_id)

    if request.method != 'POST':
        form_data.setdefault('we_added', True)

    return render(request, 'bot_app/crm/client_create.html', {
        'user': user,
        'errors': errors,
        'form_data': form_data,
    })

@ensure_csrf_cookie
@login_required_custom
def crm_client_detail(request, client_id):
    """Карточка клиента. Доступ: директор, тех. специалист (свои), бухгалтер/консультант (из чата)."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return redirect('bot_app:specialist_panel')

    with connection.cursor() as cursor:
        _ensure_users_birth_date_column(cursor)
        _ensure_added_by_columns(cursor)
        # Основные данные пользователя (все поля из таблицы users)
        _ensure_address_columns(cursor)
        cursor.execute("""
            SELECT
                u.id, u.tg_user_id, u.max_user_id, u.inn, u.org_name, u.kpp,
                u.first_name, u.last_name, u.phone, u.email, u.tg_username,
                u.city, u.address, u.birth_date, u.amo_company_id, u.we_added, u.is_verified, u.consent_accepted,
                u.consent_accepted_at, u.created_at, u.updated_at,
                u.added_by_specialist_id,
                u.added_by_partner_id, u.source_partner_id, u.partner_source_type,
                s.name as added_by_name, s.profession as added_by_profession,
                p1.name as added_by_partner_name,
                p2.name as source_partner_name
            FROM users u
            LEFT JOIN specialists s ON s.id = u.added_by_specialist_id
            LEFT JOIN specialists p1 ON p1.id = u.added_by_partner_id
            LEFT JOIN specialists p2 ON p2.id = u.source_partner_id
            WHERE u.id = %s
        """, [client_id])

        columns = [col[0] for col in cursor.description]
        row = cursor.fetchone()
        if not row:
            return render(request, 'bot_app/crm/client_not_found.html')
        client = dict(zip(columns, row))
        partner_owner_name = client.get('added_by_partner_name') or client.get('source_partner_name')
        client_inn = _normalize_inn(client.get('inn'))
        is_partner_mode = bool(user.get('is_partner'))

        # Реферальный источник (если пользователь пришёл по реф-ссылке/коду)
        referral_source = None
        try:
            cursor.execute("""
                SELECT
                    rc.id AS referral_code_id,
                    rc.code AS referral_code,
                    rc.owner_name AS referrer_name,
                    rc.owner_note AS referrer_note,
                    rc.owner_user_id AS referrer_user_id,
                    ru.first_name AS referrer_first_name,
                    ru.last_name AS referrer_last_name,
                    ra.raw_text AS raw_text,
                    ra.activated_at AS activated_at
                FROM users u
                LEFT JOIN referral_activations ra
                    ON ra.user_id COLLATE utf8mb4_unicode_ci = CAST(u.id AS CHAR) COLLATE utf8mb4_unicode_ci
                LEFT JOIN referral_codes rc ON rc.id = ra.referral_code_id
                LEFT JOIN users ru ON ru.id = rc.owner_user_id
                WHERE u.id = %s
                ORDER BY ra.activated_at DESC
                LIMIT 1
            """, [client_id])
            cols = [col[0] for col in cursor.description]
            ref_row = cursor.fetchone()
            if ref_row:
                ref_data = dict(zip(cols, ref_row))
                if ref_data.get('referral_code_id'):
                    raw_text = (ref_data.get('raw_text') or '').strip().lower()
                    invite_type = 'код'
                    if raw_text.startswith('ref_'):
                        invite_type = 'ссылка'
                    referral_source = {
                        'referral_code': ref_data.get('referral_code'),
                        'referrer_name': ref_data.get('referrer_name'),
                        'referrer_note': ref_data.get('referrer_note'),
                        'referrer_user_id': ref_data.get('referrer_user_id'),
                        'referrer_first_name': ref_data.get('referrer_first_name'),
                        'referrer_last_name': ref_data.get('referrer_last_name'),
                        'activated_at': ref_data.get('activated_at'),
                        'invite_type': invite_type,
                    }
        except Exception:
            referral_source = None

        # ККТ клиента (включая новые поля: Честный Знак, ТСПИоТ, Сервисы Прошивки)
        company_kkt_access_sql = ""
        company_kkt_access_params = []
        if is_partner_mode:
            company_kkt_access_sql = "AND co.added_by_partner_id = %s"
            company_kkt_access_params.append(user['id'])

        cursor.execute(f"""
            SELECT k.id, k.kkt_slot, k.address, k.name, k.phone, k.landmark,
                   k.kkt_registered_at, k.kkt_model, k.kkt_serial_number, k.kkt_reg_number, k.kkt_firmware,
                   k.fn_serial_number, k.fn_type, k.fn_manufacturer,
                   k.ofd_inn, k.ofd_name, k.trade_system, k.sno,
                   k.marking_enabled, k.marking_type, k.scanner_model, k.imei, k.sim_number,
                   -- Честный Знак
                   COALESCE(k.chz_registered, 0) as chz_registered, k.chz_token,
                   COALESCE(k.chz_has_ep, 0) as chz_has_ep, k.chz_product_type, k.chz_fias_id,
                   -- ТСПИоТ
                   k.tspiot_installed_at, k.tspiot_tariff_expires_at, k.tspiot_vendor,
                   -- Сервисы Прошивки
                   COALESCE(k.ecp_enabled, 0) as ecp_enabled, k.ecp_type, k.ecp_expires_at,
                   k.to_kkt_yurt, COALESCE(k.has_sim, 0) as has_sim, k.sim_payment_at,
                   k.ofd_expires_field, k.evotor_utm, k.ecp_comment,
                   -- Сроки (прямо в карточке ККТ)
                   k.fn_expires_at, k.ofd_expires_at, k.mark_expires_at,
                   k.created_at, k.updated_at,
                   source_type, source_company_id, source_company_name
            FROM (
                SELECT ck.*,
                       'client' as source_type,
                       NULL as source_company_id,
                       NULL as source_company_name
                FROM client_kkt ck
                WHERE ck.client_id = %s

                UNION ALL

                SELECT ck.*,
                       'company' as source_type,
                       co.id as source_company_id,
                       co.name as source_company_name
                FROM client_kkt ck
                JOIN company_contacts cc ON cc.company_id = ck.company_id
                JOIN companies co ON co.id = ck.company_id
                WHERE cc.contact_id = %s
                  AND (ck.client_id IS NULL OR ck.client_id <> %s)
                  {company_kkt_access_sql}
                  AND %s IS NOT NULL
                  AND REPLACE(REPLACE(REPLACE(COALESCE(co.inn, ''), ' ', ''), '-', ''), '.', '') = %s
            ) AS k
            ORDER BY k.kkt_slot
        """, [client_id, client_id, client_id, *company_kkt_access_params, client_inn, client_inn])
        kkt_columns = [col[0] for col in cursor.description]
        kkt_list = [dict(zip(kkt_columns, r)) for r in cursor.fetchall()]

        # Заказы пользователя
        cursor.execute("""
            SELECT id, amount, status, payment_id, created_at, paid_at, description, admin_note AS internal_comment, specialist_id, author_name, source
            FROM orders
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 10
        """, [client_id])
        order_columns = [col[0] for col in cursor.description]
        orders = [dict(zip(order_columns, r)) for r in cursor.fetchall()]
        for order in orders:
            order['can_edit'] = bool(
                user.get('is_director') or (
                    user.get('is_tech') and (
                        order.get('specialist_id') == user.get('id') or (
                            order.get('source') == 'manual' and order.get('author_name') == user.get('name')
                        )
                    )
                )
            )

        # Чаты поддержки
        cursor.execute("""
            SELECT sc.id, sc.status, sc.profession, sc.category, sc.created_at,
                   sp.name as specialist_name
            FROM support_chats sc
            LEFT JOIN specialists sp ON sp.id = sc.specialist_id
            WHERE sc.client_user_id = %s
            ORDER BY sc.created_at DESC
            LIMIT 10
        """, [client.get('id') or 0])
        chat_columns = [col[0] for col in cursor.description]
        chats = [dict(zip(chat_columns, r)) for r in cursor.fetchall()]

        # Кастомные поля (только для админов) - опционально
        custom_fields = []
        try:
            cursor.execute("""
                SELECT id, field_name, field_value, created_at, updated_at
                FROM user_custom_fields
                WHERE user_id = %s
                ORDER BY field_name
            """, [client_id])
            custom_columns = [col[0] for col in cursor.description]
            custom_fields = [dict(zip(custom_columns, r)) for r in cursor.fetchall()]
        except Exception:
            pass  # Таблица не существует - пропускаем

        # Компании клиента (из таблицы company_contacts)
        client_companies = []
        client_company = None
        try:
            company_access_sql = ""
            company_access_params = []
            if is_partner_mode:
                company_access_sql = "AND co.added_by_partner_id = %s"
                company_access_params.append(user['id'])

            cursor.execute(f"""
                SELECT co.id, co.name, co.inn, co.phone
                FROM company_contacts cc
                JOIN companies co ON co.id = cc.company_id
                WHERE cc.contact_id = %s
                {company_access_sql}
                ORDER BY cc.created_at ASC
            """, [client_id, *company_access_params])
            comp_cols = [col[0] for col in cursor.description]
            client_companies = [dict(zip(comp_cols, comp_row)) for comp_row in cursor.fetchall()]
            if client_companies:
                client_company = client_companies[0]
        except Exception:
            pass  # Таблицы companies/company_contacts могут не существовать

        # Самовосстановление: если у клиента заполнен org_name, но привязка к компании
        # отсутствует (например, клиент создан до внедрения auto-link) — создаём/привязываем
        if client_company is None and client.get('org_name'):
            try:
                _auto_create_or_link_company(client_id)
                # Повторный запрос — теперь привязка должна существовать
                cursor.execute("""
                    SELECT co.id, co.name, co.inn, co.phone
                    FROM company_contacts cc
                    JOIN companies co ON co.id = cc.company_id
                    WHERE cc.contact_id = %s
                      AND (%s = 0 OR co.added_by_partner_id = %s)
                    ORDER BY cc.created_at ASC
                    LIMIT 1
                """, [client_id, 1 if is_partner_mode else 0, user['id'] if is_partner_mode else None])
                comp_cols = [col[0] for col in cursor.description]
                comp_row = cursor.fetchone()
                if comp_row:
                    client_company = dict(zip(comp_cols, comp_row))
                    client_companies = [client_company]
            except Exception:
                pass

        # Финансовая информация (лояльность)
        loyalty_balance = None
        loyalty_profile = None
        loyalty_transactions = []
        try:
            cursor.execute("""
                SELECT lb.available_amount, lb.reserved_amount,
                       lb.total_earned, lb.total_spent
                FROM loyalty_balances lb
                JOIN loyalty_accounts la ON la.id = lb.account_id
                WHERE la.owner_type = 'client' AND la.owner_id = %s
            """, [client_id])
            cols = [col[0] for col in cursor.description]
            row = cursor.fetchone()
            if row:
                loyalty_balance = dict(zip(cols, row))

            cursor.execute("""
                SELECT rating, tier, total_orders, total_spent_rub, last_activity_at
                FROM user_loyalty_profile
                WHERE user_id = %s
            """, [client_id])
            cols = [col[0] for col in cursor.description]
            row = cursor.fetchone()
            if row:
                loyalty_profile = dict(zip(cols, row))

            cursor.execute("""
                SELECT lt.direction, lt.amount, lt.event_type,
                       lt.description, lt.created_at
                FROM loyalty_transactions lt
                JOIN loyalty_accounts la ON la.id = lt.account_id
                WHERE la.owner_type = 'client' AND la.owner_id = %s
                ORDER BY lt.created_at DESC
                LIMIT 10
            """, [client_id])
            cols = [col[0] for col in cursor.description]
            loyalty_transactions = [dict(zip(cols, r)) for r in cursor.fetchall()]
        except Exception:
            pass  # Таблицы лояльности могут не существовать

        # Заявка на открытие ИП через Альфа-Банк
        alfabank_ip_application = None
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
            if row:
                alfabank_ip_application = dict(zip(cols, row))
        except Exception:
            pass  # Таблица может не существовать

    return render(request, 'bot_app/crm/client_detail.html', {
        'client': client,
        'kkt_list': kkt_list,
        'orders': orders,
        'chats': chats,
        'referral_source': referral_source,
        'custom_fields': custom_fields,
        'loyalty_balance': loyalty_balance,
        'loyalty_profile': loyalty_profile,
        'loyalty_transactions': loyalty_transactions,
        'client_company': client_company,
        'client_companies': client_companies,
        'partner_mode': is_partner_mode,
        'active_menu': 'clients',
        'today': date.today(),
        'user': user,
        'partner_owner_name': partner_owner_name,
        'alfabank_ip_application': alfabank_ip_application,
        'can_edit_specialist': bool(user.get('is_director')),
        'specialists_for_edit': _get_all_specialists() if user.get('is_director') else [],
    })

@login_required_custom
@require_POST
def api_alfabank_ip_refresh(request, client_id):
    """Обновить статус заявки ИП из Альфа-Банк API и вернуть актуальные данные."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    return JsonResponse(refresh_alfabank_ip(user, client_id))

@login_required_custom
@require_POST
def crm_client_edit(request, client_id):
    """API для редактирования данных клиента. Любой специалист — только клиентов из своих чатов/созданных."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    try:
        data = json.loads(request.body)
        field = data.get('field')
        value = data.get('value')

        # Разрешённые поля для редактирования (данные бота)
        allowed_fields = [
            'first_name', 'last_name', 'phone', 'email', 'inn',
            'org_name', 'kpp', 'city', 'address', 'birth_date', 'tg_username', 'amo_company_id', 'we_added'
        ]

        if field not in allowed_fields:
            return JsonResponse({'success': False, 'error': 'Поле не разрешено для редактирования'})

        # Обновляем поле в БД
        with connection.cursor() as cursor:
            _ensure_users_birth_date_column(cursor)
            # Проверяем существует ли клиент
            cursor.execute("SELECT id FROM users WHERE id = %s", [client_id])
            if not cursor.fetchone():
                return JsonResponse({'success': False, 'error': 'Клиент не найден'})

            db_value = value if value else None
            if field == 'we_added':
                if isinstance(value, str):
                    normalized = value.strip().lower()
                    is_true = normalized in {'1', 'true', 'yes', 'on'}
                else:
                    is_true = bool(value)
                db_value = 1 if is_true else 0
            elif field == 'inn':
                normalized_inn = _normalize_inn(db_value)
                db_value = normalized_inn or None
                if db_value:
                    cursor.execute(
                        "SELECT id FROM users WHERE inn = %s AND id != %s LIMIT 1",
                        [db_value, client_id],
                    )
                    if cursor.fetchone():
                        return JsonResponse({
                            'success': False,
                            'error': 'Извините, клиент с таким ИНН уже есть в базе.'
                        })
            elif field == 'phone' and db_value is not None:
                digits = ''.join(ch for ch in str(db_value) if ch.isdigit())
                if len(digits) == 10 and digits.startswith('9'):
                    digits = '7' + digits
                if len(digits) == 11 and digits.startswith('8'):
                    digits = '7' + digits[1:]
                db_value = f"+{digits}" if len(digits) == 11 and digits.startswith('7') else None
            elif field == 'birth_date' and db_value is not None:
                birth_date_raw = str(db_value).strip()
                try:
                    db_value = datetime.strptime(birth_date_raw, '%Y-%m-%d').date()
                except Exception:
                    return JsonResponse({'success': False, 'error': 'Некорректная дата. Используйте формат ГГГГ-ММ-ДД'})

            # Обновляем поле
            cursor.execute(f"""
                UPDATE users SET {field} = %s, updated_at = NOW() WHERE id = %s
            """, [db_value, client_id])

            # is_verified валиден только если одновременно заполнены корректные ИНН + телефон
            if field in ('inn', 'phone'):
                cursor.execute("""
                    UPDATE users
                       SET is_verified = CASE
                           WHEN inn IS NOT NULL AND TRIM(inn) != ''
                            AND phone IS NOT NULL AND TRIM(phone) != ''
                           THEN 1 ELSE 0 END,
                           updated_at = NOW()
                     WHERE id = %s
                """, [client_id])

            # Авто-создание/обновление компании при изменении org_name, inn, kpp, phone, email, city
            if field in ('org_name', 'inn', 'kpp', 'phone', 'email', 'city'):
                _auto_create_or_link_company(client_id, cursor)

        return JsonResponse({'success': True, 'message': 'Данные обновлены'})

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Неверный формат данных'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required_custom
@require_POST
def crm_custom_field_save(request, client_id):
    """API для сохранения кастомного поля. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Неверный формат данных'})
    return JsonResponse(save_custom_field(user, client_id, data))

@login_required_custom
@require_POST
def crm_custom_field_delete(request, client_id, field_id):
    """API для удаления кастомного поля. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    return JsonResponse(delete_custom_field(user, client_id, field_id))

@login_required_custom
@require_POST
def crm_kkt_add(request, client_id):
    """Добавить ККТ клиенту. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse(add_client_kkt(user, client_id, data))

@login_required_custom
@require_POST
def crm_kkt_delete(request, client_id, kkt_id):
    """Удалить ККТ. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    return JsonResponse(delete_client_kkt(user, client_id, kkt_id))

@login_required_custom
@require_POST
def crm_kkt_update(request, client_id, kkt_id):
    """Обновить поле ККТ. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse(update_client_kkt(user, client_id, kkt_id, data))

@login_required_custom
def api_client_notes(request, client_id):
    """Получить заметки и сделки клиента. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа', 'notes': []})
    return JsonResponse(get_client_notes(client_id))

@login_required_custom
@require_POST
def api_client_note_save(request, client_id):
    """Создать заметку или сделку. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse(save_client_note(user, client_id, data))

@login_required_custom
@require_POST
def api_client_note_delete(request, client_id, note_id):
    """Удалить заметку или сделку. Специалист — только для своих клиентов."""
    user = get_current_user(request)
    if not _check_specialist_client_access(user, client_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому клиенту'})
    source_table = 'client_notes'
    if request.body:
        try:
            data = json.loads(request.body)
            source_table = data.get('source_table', 'client_notes')
        except (json.JSONDecodeError, ValueError):
            pass
    return JsonResponse(delete_client_note(user, client_id, note_id, source_table))
