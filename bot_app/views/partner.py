"""Partner cabinet views extracted from the legacy views module."""

import json
import logging
import os
from datetime import date

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

import db as db_async
from ..permissions import (
    get_current_user,
    login_required_custom,
    partner_required,
)
from ..sql_cursor import with_sql_rewrite
from .chats import _ensure_support_chats_time_columns
from .clients import crm_client_detail
from .common import _normalize_inn, _normalize_phone_digits
from .company_schema import _ensure_added_by_columns, _ensure_companies_tables

connection = with_sql_rewrite(connection)
logger = logging.getLogger(__name__)



def _find_or_link_partner_user_id(partner_specialist_id, force_relink=False):
    """
    Возвращает users.id, связанный с partner-specialist.
    Приоритет: linked_user_id -> tg/max id -> phone -> создаём users.
    force_relink=True игнорирует cached linked_user_id (нужно при смене телефона).
    """
    with connection.cursor() as cursor:
        # best-effort колонка связки
        try:
            cursor.execute("ALTER TABLE specialists ADD COLUMN linked_user_id BIGINT NULL DEFAULT NULL")
        except Exception:
            pass

        cursor.execute("""
            SELECT linked_user_id, tg_user_id, max_user_id, phone, name
            FROM specialists
            WHERE id=%s AND profession=5
            LIMIT 1
        """, [partner_specialist_id])
        row = cursor.fetchone()
        if not row:
            return None

        linked_user_id, tg_user_id, max_user_id, phone, spec_name = row

        if linked_user_id and not force_relink:
            cursor.execute("SELECT id FROM users WHERE id=%s LIMIT 1", [linked_user_id])
            if cursor.fetchone():
                return int(linked_user_id)

        if tg_user_id:
            cursor.execute("SELECT id FROM users WHERE tg_user_id=%s LIMIT 1", [tg_user_id])
            u = cursor.fetchone()
            if u:
                user_id = int(u[0])
                cursor.execute("UPDATE specialists SET linked_user_id=%s WHERE id=%s", [user_id, partner_specialist_id])
                return user_id

        if max_user_id:
            cursor.execute("SELECT id FROM users WHERE max_user_id=%s LIMIT 1", [max_user_id])
            u = cursor.fetchone()
            if u:
                user_id = int(u[0])
                cursor.execute("UPDATE specialists SET linked_user_id=%s WHERE id=%s", [user_id, partner_specialist_id])
                return user_id

        phone_digits = _normalize_phone_digits(phone)
        if phone_digits:
            cursor.execute(
                """
                SELECT id
                FROM users
                WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'(',''),')',''),'-',''),' ','')=%s
                ORDER BY id ASC
                LIMIT 1
                """,
                [phone_digits],
            )
            u = cursor.fetchone()
            if u:
                user_id = int(u[0])
                cursor.execute("UPDATE specialists SET linked_user_id=%s WHERE id=%s", [user_id, partner_specialist_id])
                return user_id

            # Создаём users-запись для партнёра, которого ещё нет в боте.
            # Телефон нормализуем в формат +7XXXXXXXXXX чтобы find_existing_user_for_link
            # нашёл запись при регистрации партнёра в боте и объединил аккаунты.
            phone_stored = ('+' + phone_digits) if len(phone_digits) == 11 else phone
            cursor.execute(
                """
                INSERT INTO users (first_name, phone, we_added, created_at, updated_at)
                VALUES (%s, %s, 1, NOW(), NOW())
                """,
                [spec_name or 'Партнёр', phone_stored],
            )
            user_id = cursor.lastrowid
            if user_id:
                cursor.execute("UPDATE specialists SET linked_user_id=%s WHERE id=%s", [user_id, partner_specialist_id])
                return user_id
    return None


def _is_partner_client_accessible(partner_id, client_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM users
            WHERE id = %s
              AND (added_by_partner_id = %s OR source_partner_id = %s)
            LIMIT 1
            """,
            [client_id, partner_id, partner_id],
        )
        return cursor.fetchone() is not None


def _is_partner_company_accessible(partner_id, company_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM companies
            WHERE id = %s
              AND added_by_partner_id = %s
            LIMIT 1
            """,
            [company_id, partner_id],
        )
        return cursor.fetchone() is not None


def _ensure_partner_director_chat_for_site(partner_specialist_id):
    """
    Гарантирует постоянный чат партнёра с директором при входе на сайт.
    Возвращает users.id партнёра или None, если связку определить не удалось.
    """
    partner_user_id = _find_or_link_partner_user_id(partner_specialist_id)
    if not partner_user_id:
        return None
    try:
        async_to_sync(db_async.ensure_partner_director_chat)(
            partner_specialist_id=int(partner_specialist_id),
            partner_user_id=int(partner_user_id),
            source="site",
        )
    except Exception:
        logger.debug("ensure_partner_director_chat_for_site failed for specialist=%s", partner_specialist_id, exc_info=True)
    return int(partner_user_id)


def _resolve_partner_identity(cursor, *, client_user_id=None, client_phone=None, fallback_name=''):
    """Возвращает данные партнёра (specialists.profession=5) для отображения в чатах."""
    partner = None
    if client_user_id:
        cursor.execute(
            """
            SELECT id, name, phone
            FROM specialists
            WHERE profession = 5
              AND linked_user_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            [int(client_user_id)],
        )
        row = cursor.fetchone()
        if row:
            partner = {'id': int(row[0]), 'name': (row[1] or '').strip(), 'phone': row[2]}

    if not partner and client_phone:
        phone_digits = _normalize_phone_digits(client_phone)
        if phone_digits:
            cursor.execute(
                """
                SELECT id, name, phone
                FROM specialists
                WHERE profession = 5
                  AND REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'(',''),')',''),'-',''),' ','') = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                [phone_digits],
            )
            row = cursor.fetchone()
            if row:
                partner = {'id': int(row[0]), 'name': (row[1] or '').strip(), 'phone': row[2]}

    partner_name = (partner or {}).get('name') or (fallback_name or '').strip() or 'Партнёр'
    return {
        'partner_specialist_id': (partner or {}).get('id'),
        'partner_name': partner_name,
        'partner_phone': (partner or {}).get('phone') or client_phone,
    }


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_clients_list(request):
    user = get_current_user(request)
    _ensure_partner_director_chat_for_site(user['id'])
    _ensure_added_by_columns()
    search = (request.GET.get('search') or '').strip()
    source_filter = (request.GET.get('source') or '').strip().lower()
    status_filter = (request.GET.get('status') or '').strip().lower()

    with connection.cursor() as cursor:
        where = "WHERE (u.added_by_partner_id = %s OR u.source_partner_id = %s)"
        params = [user['id'], user['id']]
        if search:
            where += """
              AND (
                u.inn LIKE %s OR u.org_name LIKE %s OR u.phone LIKE %s
                OR u.first_name LIKE %s OR u.last_name LIKE %s
              )
            """
            params.extend([f'%{search}%'] * 5)
        if source_filter in ('manual', 'referral'):
            where += " AND u.partner_source_type = %s"
            params.append(source_filter)
        if status_filter == 'verified':
            where += " AND u.is_verified = 1"
        elif status_filter == 'unverified':
            where += " AND (u.is_verified = 0 OR u.is_verified IS NULL)"
        elif status_filter == 'tg':
            where += " AND u.tg_user_id IS NOT NULL"
        elif status_filter == 'max':
            where += " AND u.max_user_id IS NOT NULL"

        cursor.execute(f"""
            SELECT
              u.id, u.first_name, u.last_name, u.org_name, u.phone, u.inn,
              u.tg_user_id, u.max_user_id, u.is_verified, u.partner_source_type, u.created_at
            FROM users u
            {where}
            ORDER BY u.created_at DESC
            LIMIT 200
        """, params)
        cols = [col[0] for col in cursor.description]
        clients = [dict(zip(cols, row)) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT
              COUNT(*) AS total_clients,
              SUM(CASE WHEN tg_user_id IS NOT NULL THEN 1 ELSE 0 END) AS tg_activated,
              SUM(CASE WHEN max_user_id IS NOT NULL THEN 1 ELSE 0 END) AS max_activated,
              SUM(CASE WHEN is_verified = 1 THEN 1 ELSE 0 END) AS verified_count
            FROM users
            WHERE added_by_partner_id = %s OR source_partner_id = %s
        """, [user['id'], user['id']])
        stats_row = cursor.fetchone()
        stats_cols = [col[0] for col in cursor.description]
        stats = dict(zip(stats_cols, stats_row)) if stats_row else {}

    return render(request, 'bot_app/partner_clients_list.html', {
        'user': user,
        'clients': clients,
        'search': search,
        'source_filter': source_filter,
        'status_filter': status_filter,
        'stats': stats,
    })


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_client_create(request):
    user = get_current_user(request)
    _ensure_added_by_columns()
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
        if not form_data['first_name'] and not form_data['org_name']:
            errors['first_name'] = 'Укажите имя или название компании'
        if not errors:
            with connection.cursor() as cursor:
                if normalized_inn:
                    cursor.execute("SELECT id FROM users WHERE inn = %s LIMIT 1", [normalized_inn])
                    if cursor.fetchone():
                        errors['inn'] = 'Извините, клиент с таким ИНН уже есть в базе.'

                existing_id = None
                if not errors and form_data.get('phone'):
                    cursor.execute("SELECT id FROM users WHERE phone = %s LIMIT 1", [form_data['phone']])
                    row = cursor.fetchone()
                    if row:
                        existing_id = int(row[0])
                if existing_id:
                    cursor.execute("SELECT added_by_partner_id, source_partner_id FROM users WHERE id=%s", [existing_id])
                    owner = cursor.fetchone() or (None, None)
                    if owner[0] and int(owner[0]) != int(user['id']):
                        errors['phone'] = 'Клиент уже закреплён за другим партнёром'
                    elif owner[1] and int(owner[1]) != int(user['id']):
                        errors['phone'] = 'Клиент уже пришёл по ссылке другого партнёра'
                    else:
                        cursor.execute("""
                            UPDATE users
                            SET added_by_partner_id = COALESCE(added_by_partner_id, %s),
                                partner_source_type = COALESCE(partner_source_type, 'manual'),
                                updated_at = NOW()
                            WHERE id = %s
                        """, [user['id'], existing_id])
                        return redirect('bot_app:partner_client_detail', client_id=existing_id)
                else:
                    cursor.execute("""
                        INSERT INTO users (
                            first_name, last_name, phone, email, inn,
                            org_name, kpp, city, tg_username,
                            we_added, consent_accepted, is_verified,
                            added_by_partner_id, partner_source_type, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, 0, 0,
                            %s, 'manual', NOW(), NOW()
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
                    created_id = int(cursor.fetchone()[0])
                    return redirect('bot_app:partner_client_detail', client_id=created_id)

    return render(request, 'bot_app/crm/client_create.html', {
        'user': user,
        'errors': errors,
        'form_data': form_data,
        'partner_mode': True,
    })


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_client_detail(request, client_id):
    user = get_current_user(request)
    if not _is_partner_client_accessible(user['id'], client_id):
        return redirect('bot_app:partner_clients')
    return crm_client_detail(request, client_id)


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_chats_list(request):
    user = get_current_user(request)
    partner_user_id = _ensure_partner_director_chat_for_site(user['id'])

    if not partner_user_id:
        return render(request, 'bot_app/partner_chats_list.html', {
            'user': user,
            'chats': [],
            'partner_user_id': None,
            'active_page': 'partner_chats',
        })

    with connection.cursor() as cursor:
        _ensure_support_chats_time_columns(cursor)
        cursor.execute("""
            SELECT
                sc.id, sc.client_user_id, sc.specialist_id, sc.status, sc.profession,
                sc.category, sc.request_type, sc.source, sc.created_at, sc.updated_at,
                s.name AS specialist_name,
                (
                    SELECT COUNT(*)
                    FROM support_chat_messages m
                    WHERE m.chat_id = sc.id
                      AND m.sender = 1
                      AND m.id > COALESCE((
                          SELECT MAX(m2.id)
                          FROM support_chat_messages m2
                          WHERE m2.chat_id = sc.id AND m2.sender = 0
                      ), 0)
                ) AS unread_for_partner
            FROM support_chats sc
            LEFT JOIN specialists s ON s.id = sc.specialist_id
            WHERE sc.client_user_id = %s
              AND (sc.request_type IS NULL OR sc.request_type != 'order')
              AND sc.status IN ('pending', 'active', 'consultation', 'closed', 'test')
            ORDER BY
                CASE WHEN sc.request_type = 'partner_director' THEN 0 ELSE 1 END,
                sc.updated_at DESC,
                sc.id DESC
        """, [int(partner_user_id)])
        columns = [col[0] for col in cursor.description]
        chats = [dict(zip(columns, row)) for row in cursor.fetchall()]
        for c in chats:
            c['has_new_messages'] = int(c.get('unread_for_partner') or 0) > 0

    return render(request, 'bot_app/partner_chats_list.html', {
        'user': user,
        'chats': chats,
        'partner_user_id': int(partner_user_id),
        'active_page': 'partner_chats',
    })


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_analytics(request):
    user = get_current_user(request)
    _ensure_added_by_columns()

    tg_bot = (getattr(settings, 'TG_REF_BOT_USERNAME', '') or os.getenv('TG_REF_BOT_USERNAME', '')).strip().lstrip('@')
    max_bot = (os.getenv('MAX_REF_BOT_NAME', '') or os.getenv('MAX_BOT_NAME', '') or os.getenv('MAX_REF_FALLBACK_BOT_NAME', '')).strip().lstrip('@')

    owner_name = (f"{(user.get('first_name') or '').strip()} {(user.get('last_name') or '').strip()}").strip()
    if not owner_name:
        owner_name = (user.get("name") or "").strip() or f"Специалист #{user['id']}"
    ref_code = async_to_sync(db_async.get_or_create_specialist_ref_code)(int(user["id"]), owner_name=owner_name)

    with connection.cursor() as cursor:

        cursor.execute("""
            SELECT
              COUNT(*) AS total_clients,
              SUM(CASE WHEN added_by_partner_id = %s THEN 1 ELSE 0 END) AS manual_count,
              SUM(CASE WHEN source_partner_id = %s THEN 1 ELSE 0 END) AS referral_count,
              SUM(CASE WHEN (added_by_partner_id = %s OR source_partner_id = %s) AND tg_user_id IS NOT NULL THEN 1 ELSE 0 END) AS tg_activated,
              SUM(CASE WHEN (added_by_partner_id = %s OR source_partner_id = %s) AND max_user_id IS NOT NULL THEN 1 ELSE 0 END) AS max_activated,
              SUM(CASE WHEN (added_by_partner_id = %s OR source_partner_id = %s) AND is_verified = 1 THEN 1 ELSE 0 END) AS verified_count
            FROM users
            WHERE added_by_partner_id = %s OR source_partner_id = %s
        """, [user['id'], user['id'], user['id'], user['id'], user['id'], user['id'], user['id'], user['id'], user['id'], user['id']])
        stats = dict(zip([c[0] for c in cursor.description], cursor.fetchone()))

        cursor.execute("""
            SELECT COUNT(*) AS deals_count, COALESCE(SUM(amount), 0) AS deals_amount
            FROM orders o
            WHERE EXISTS (
                SELECT 1 FROM users u
                WHERE u.id = o.user_id
                  AND (u.added_by_partner_id = %s OR u.source_partner_id = %s)
            )
        """, [user['id'], user['id']])
        deals = dict(zip([c[0] for c in cursor.description], cursor.fetchone()))

        cursor.execute("""
            SELECT id, first_name, last_name, phone, tg_user_id, max_user_id, is_verified, created_at
            FROM users
            WHERE source_partner_id = %s
            ORDER BY created_at DESC
            LIMIT 100
        """, [user['id']])
        referred_clients = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]

    tg_ref_url = f"https://t.me/{tg_bot}?start=ref_{ref_code}" if (tg_bot and ref_code) else None
    max_ref_url = f"https://max.ru/{max_bot}?start=ref_{ref_code}" if (max_bot and ref_code) else None

    return render(request, 'bot_app/partner_analytics.html', {
        'user': user,
        'ref_code': ref_code,
        'tg_ref_url': tg_ref_url,
        'max_ref_url': max_ref_url,
        'stats': stats,
        'bot_activated': (int(stats.get('tg_activated') or 0) + int(stats.get('max_activated') or 0)),
        'deals': deals,
        'referred_clients': referred_clients,
    })


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_companies_list(request):
    user = get_current_user(request)
    _ensure_partner_director_chat_for_site(user['id'])
    _ensure_companies_tables()
    search = (request.GET.get('search') or '').strip()

    with connection.cursor() as cursor:
        params = [user['id']]
        where = "WHERE c.added_by_partner_id = %s"
        if search:
            where += " AND (c.name LIKE %s OR c.inn LIKE %s OR c.address LIKE %s OR c.phone LIKE %s)"
            params.extend([f'%{search}%'] * 4)

        cursor.execute(f"""
            SELECT
                c.id, c.name, c.inn, c.address, c.phone, c.created_at,
                (SELECT COUNT(*) FROM company_contacts cc WHERE cc.company_id = c.id) AS contacts_count,
                (SELECT COUNT(*) FROM client_kkt ck WHERE ck.company_id = c.id) AS kkt_count
            FROM companies c
            {where}
            ORDER BY c.created_at DESC
            LIMIT 200
        """, params)
        columns = [col[0] for col in cursor.description]
        companies = [dict(zip(columns, row)) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT
                COUNT(*) AS total_companies,
                SUM(CASE WHEN inn IS NOT NULL AND inn <> '' THEN 1 ELSE 0 END) AS with_inn,
                COALESCE(SUM((SELECT COUNT(*) FROM company_contacts cc WHERE cc.company_id = c.id)), 0) AS linked_clients,
                SUM(CASE WHEN EXISTS (SELECT 1 FROM client_kkt ck WHERE ck.company_id = c.id) THEN 1 ELSE 0 END) AS active_with_kkt
            FROM companies c
            WHERE c.added_by_partner_id = %s
        """, [user['id']])
        stats = dict(zip([col[0] for col in cursor.description], cursor.fetchone() or (0, 0, 0, 0)))

    return render(request, 'bot_app/partner_companies_list.html', {
        'user': user,
        'companies': companies,
        'search': search,
        'stats': stats,
    })


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_company_create(request):
    user = get_current_user(request)
    _ensure_companies_tables()
    errors = {}
    form_data = {}

    if request.method == 'POST':
        form_data = {
            'name': request.POST.get('name', '').strip(),
            'inn': _normalize_inn(request.POST.get('inn', '')),
            'kpp': request.POST.get('kpp', '').strip(),
            'ogrn': request.POST.get('ogrn', '').strip(),
            'address': request.POST.get('address', '').strip(),
            'phone': request.POST.get('phone', '').strip(),
            'email': request.POST.get('email', '').strip(),
            'website': request.POST.get('website', '').strip(),
            'description': request.POST.get('description', '').strip(),
            'note': request.POST.get('note', '').strip(),
        }

        if not form_data['name']:
            errors['name'] = 'Название компании обязательно'

        if not errors:
            with connection.cursor() as cursor:
                if form_data['inn']:
                    cursor.execute("SELECT id FROM companies WHERE inn = %s LIMIT 1", [form_data['inn']])
                    if cursor.fetchone():
                        errors['inn'] = 'Извините, компания с таким ИНН уже есть в базе.'

                if not errors:
                    cursor.execute("""
                        INSERT INTO companies (
                            name, inn, kpp, ogrn, address, phone, email, website,
                            description, note, added_by_partner_id, partner_source_type
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual')
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
                    new_id = int(cursor.fetchone()[0])
                    return redirect('bot_app:partner_company_detail', company_id=new_id)

    return render(request, 'bot_app/crm/company_create.html', {
        'user': user,
        'errors': errors,
        'form_data': form_data,
        'partner_mode': True,
    })


@ensure_csrf_cookie
@login_required_custom
@partner_required
def partner_company_detail(request, company_id):
    user = get_current_user(request)
    if not _is_partner_company_accessible(user['id'], company_id):
        return redirect('bot_app:partner_companies')
    _ensure_companies_tables()

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
            WHERE c.id = %s AND c.added_by_partner_id = %s
        """, [company_id, user['id']])
        columns = [col[0] for col in cursor.description]
        row = cursor.fetchone()
        if not row:
            return redirect('bot_app:partner_companies')
        company = dict(zip(columns, row))

        cursor.execute("""
            SELECT cc.id as link_id, cc.created_at as linked_at,
                   u.id as contact_id, u.first_name, u.last_name, u.phone, u.email,
                   u.tg_username, u.inn as contact_inn, u.org_name
            FROM company_contacts cc
            JOIN users u ON u.id = cc.contact_id
            WHERE cc.company_id = %s
              AND (u.added_by_partner_id = %s OR u.source_partner_id = %s)
            ORDER BY cc.created_at DESC
        """, [company_id, user['id'], user['id']])
        contacts = [dict(zip([col[0] for col in cursor.description], r)) for r in cursor.fetchall()]

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
        kkt_list = [dict(zip([col[0] for col in cursor.description], r)) for r in cursor.fetchall()]

    return render(request, 'bot_app/crm/company_detail.html', {
        'company': company,
        'contacts': contacts,
        'kkt_list': kkt_list,
        'orders': [],
        'active_menu': 'companies',
        'today': date.today(),
        'user': user,
        'partner_mode': True,
        'companies_list_url': '/partner/companies/',
        'company_api_base': '/partner/companies/',
        'contacts_search_url': '/api/partner/contacts/search/',
    })


@login_required_custom
@partner_required
@require_POST
def partner_company_edit(request, company_id):
    user = get_current_user(request)
    _ensure_companies_tables()
    if not _is_partner_company_accessible(user['id'], company_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Неверный формат данных'})

    field = data.get('field')
    value = data.get('value')
    allowed_fields = ['name', 'inn', 'kpp', 'ogrn', 'address', 'phone', 'email', 'website', 'description', 'note']
    if field not in allowed_fields:
        return JsonResponse({'success': False, 'error': 'Поле не разрешено для редактирования'})

    with connection.cursor() as cursor:
        if field == 'inn':
            normalized_inn = _normalize_inn(value)
            if normalized_inn:
                cursor.execute("SELECT id FROM companies WHERE inn = %s AND id != %s LIMIT 1", [normalized_inn, company_id])
                if cursor.fetchone():
                    return JsonResponse({'success': False, 'error': 'Извините, компания с таким ИНН уже есть в базе.'})
            value = normalized_inn or None
        cursor.execute(
            f"UPDATE companies SET {field} = %s, updated_at = NOW() WHERE id = %s AND added_by_partner_id = %s",
            [value if value else None, company_id, user['id']]
        )
    return JsonResponse({'success': True, 'message': 'Данные обновлены'})


@login_required_custom
@partner_required
@require_POST
def partner_company_delete(request, company_id):
    user = get_current_user(request)
    _ensure_companies_tables()
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM companies WHERE id = %s AND added_by_partner_id = %s", [company_id, user['id']])
        if not cursor.fetchone():
            return JsonResponse({'success': False, 'error': 'Компания не найдена'}, status=404)
        cursor.execute("DELETE FROM companies WHERE id = %s AND added_by_partner_id = %s", [company_id, user['id']])
    return JsonResponse({'success': True, 'message': 'Компания удалена'})


@login_required_custom
@partner_required
@require_POST
def partner_company_contact_add(request, company_id):
    user = get_current_user(request)
    _ensure_companies_tables()
    if not _is_partner_company_accessible(user['id'], company_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Неверный формат данных'})

    contact_id = data.get('contact_id')
    with connection.cursor() as cursor:
        if contact_id:
            cursor.execute("""
                SELECT id FROM users
                WHERE id = %s
                  AND (added_by_partner_id = %s OR source_partner_id = %s)
                LIMIT 1
            """, [contact_id, user['id'], user['id']])
            if not cursor.fetchone():
                return JsonResponse({'success': False, 'error': 'Клиент не найден или недоступен'}, status=403)
        else:
            first_name = (data.get('first_name') or '').strip()
            last_name = (data.get('last_name') or '').strip()
            phone = (data.get('phone') or '').strip()
            email = (data.get('email') or '').strip()
            inn = _normalize_inn(data.get('inn'))
            org_name = (data.get('org_name') or '').strip()

            if not first_name and not org_name:
                return JsonResponse({'success': False, 'error': 'Укажите имя клиента'}, status=400)

            if inn:
                cursor.execute("SELECT id FROM users WHERE inn = %s LIMIT 1", [inn])
                if cursor.fetchone():
                    return JsonResponse({'success': False, 'error': 'Извините, клиент с таким ИНН уже есть в базе.'}, status=400)

            existing_id = None
            if phone:
                cursor.execute("SELECT id FROM users WHERE phone = %s LIMIT 1", [phone])
                row = cursor.fetchone()
                if row:
                    existing_id = int(row[0])

            if existing_id:
                cursor.execute("SELECT added_by_partner_id, source_partner_id FROM users WHERE id=%s", [existing_id])
                owner = cursor.fetchone() or (None, None)
                if owner[0] and int(owner[0]) != int(user['id']):
                    return JsonResponse({'success': False, 'error': 'Клиент уже закреплён за другим партнёром'}, status=400)
                if owner[1] and int(owner[1]) != int(user['id']):
                    return JsonResponse({'success': False, 'error': 'Клиент уже пришёл по ссылке другого партнёра'}, status=400)

                cursor.execute("""
                    UPDATE users
                    SET added_by_partner_id = COALESCE(added_by_partner_id, %s),
                        partner_source_type = COALESCE(partner_source_type, 'manual'),
                        updated_at = NOW()
                    WHERE id = %s
                """, [user['id'], existing_id])
                contact_id = existing_id
            else:
                cursor.execute("""
                    INSERT INTO users (
                        first_name, last_name, phone, email, inn, org_name,
                        we_added, consent_accepted, is_verified,
                        added_by_partner_id, partner_source_type, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, 0, 0,
                        %s, 'manual', NOW(), NOW()
                    )
                """, [
                    first_name or None,
                    last_name or None,
                    phone or None,
                    email or None,
                    inn or None,
                    org_name or None,
                    1,
                    user['id'],
                ])
                cursor.execute("SELECT LAST_INSERT_ID()")
                contact_id = int(cursor.fetchone()[0])

        cursor.execute("""
            SELECT id FROM company_contacts
            WHERE company_id = %s AND contact_id = %s
            LIMIT 1
        """, [company_id, contact_id])
        if cursor.fetchone():
            return JsonResponse({'success': False, 'error': 'Этот клиент уже добавлен в компанию.'}, status=400)

        cursor.execute("""
            INSERT INTO company_contacts (company_id, contact_id)
            VALUES (%s, %s)
        """, [company_id, contact_id])
    return JsonResponse({'success': True, 'message': 'Клиент привязан', 'contact_id': int(contact_id)})


@login_required_custom
@partner_required
@require_POST
def partner_company_contact_remove(request, company_id, contact_id):
    user = get_current_user(request)
    _ensure_companies_tables()
    if not _is_partner_company_accessible(user['id'], company_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)
    with connection.cursor() as cursor:
        cursor.execute("""
            DELETE cc
            FROM company_contacts cc
            JOIN users u ON u.id = cc.contact_id
            WHERE cc.company_id = %s
              AND cc.contact_id = %s
              AND (u.added_by_partner_id = %s OR u.source_partner_id = %s)
        """, [company_id, contact_id, user['id'], user['id']])
        if cursor.rowcount == 0:
            return JsonResponse({'success': False, 'error': 'Связь не найдена или недоступна'}, status=404)
    return JsonResponse({'success': True, 'message': 'Клиент отвязан'})


@login_required_custom
@partner_required
@require_POST
def partner_company_kkt_add(request, company_id):
    user = get_current_user(request)
    _ensure_companies_tables()
    if not _is_partner_company_accessible(user['id'], company_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)
    try:
        data = json.loads(request.body)
        kkt_slot = data.get('kkt_slot')
        if not kkt_slot:
            return JsonResponse({'success': False, 'error': 'Укажите номер слота'})

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
            new_id = cursor.fetchone()[0]
        return JsonResponse({'success': True, 'message': 'ККТ добавлен', 'kkt_id': new_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required_custom
@partner_required
@require_POST
def partner_company_kkt_delete(request, company_id, kkt_id):
    user = get_current_user(request)
    _ensure_companies_tables()
    if not _is_partner_company_accessible(user['id'], company_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM client_kkt WHERE id = %s AND company_id = %s", [kkt_id, company_id])
        return JsonResponse({'success': True, 'message': 'ККТ удалён'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required_custom
@partner_required
@require_POST
def partner_company_kkt_update(request, company_id, kkt_id):
    user = get_current_user(request)
    _ensure_companies_tables()
    if not _is_partner_company_accessible(user['id'], company_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)
    try:
        data = json.loads(request.body)
        field = data.get('field')
        value = data.get('value')

        allowed_fields = [
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
        bool_fields = ['marking_enabled', 'chz_registered', 'chz_has_ep', 'ecp_enabled', 'has_sim']
        if field not in allowed_fields:
            return JsonResponse({'success': False, 'error': 'Поле не разрешено'})

        with connection.cursor() as cursor:
            if field in bool_fields:
                value = 1 if value in ['1', 'true', True, 1] else 0
            db_value = value if value else None
            cursor.execute(f"""
                UPDATE client_kkt SET {field} = %s, updated_at = NOW()
                WHERE id = %s AND company_id = %s
            """, [db_value, kkt_id, company_id])
        return JsonResponse({'success': True, 'message': 'Данные обновлены'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required_custom
@partner_required
def api_partner_contacts_search(request):
    q = request.GET.get('q', '').strip()
    user = get_current_user(request)
    if len(q) < 2:
        return JsonResponse({'success': True, 'contacts': []})

    phone_digits = ''.join(ch for ch in q if ch.isdigit())
    phone_like_param = None
    if len(phone_digits) >= 3:
        if len(phone_digits) == 11 and phone_digits.startswith('8'):
            phone_digits = f"7{phone_digits[1:]}"
        elif len(phone_digits) == 10 and phone_digits.startswith('9'):
            phone_digits = f"7{phone_digits}"
        phone_like_param = f"%{phone_digits}%"

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, first_name, last_name, phone, email, tg_username, inn, org_name
            FROM users
            WHERE (added_by_partner_id = %s OR source_partner_id = %s)
              AND (
                first_name LIKE %s
                OR last_name LIKE %s
                OR phone LIKE %s
                OR email LIKE %s
                OR inn LIKE %s
                OR org_name LIKE %s
                OR (
                    %s IS NOT NULL
                    AND REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),'.','') LIKE %s
                )
              )
            ORDER BY first_name
            LIMIT 20
        """, [user['id'], user['id'], f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%', phone_like_param, phone_like_param])
        contacts = [dict(zip([col[0] for col in cursor.description], row)) for row in cursor.fetchall()]

    return JsonResponse({'success': True, 'contacts': contacts})
