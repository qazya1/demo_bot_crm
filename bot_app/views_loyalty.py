"""
Views для финансовой модели: кабинет директора + кабинет партнёра.

Стиль: raw SQL (как в views.py), самодостаточные шаблоны.
"""
import csv
import io
import json
import logging
from datetime import datetime
from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.db import connection, transaction
from django.views.decorators.http import require_POST
from .sql_cursor import with_sql_rewrite

logger = logging.getLogger(__name__)
connection = with_sql_rewrite(connection)


# ==================== Helpers ====================

def _get_current_user(request):
    """Копия из views.py — получить авторизованного пользователя."""
    specialist_id = request.session.get('specialist_id')
    if not specialist_id:
        return None
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, name, login, is_director, profession, phone, tg_username
            FROM specialists WHERE id = %s
        """, [specialist_id])
        row = cursor.fetchone()
        if row:
            is_director = bool(row[3])
            return {
                'id': row[0], 'name': row[1], 'login': row[2],
                'is_director': is_director,
                'is_specialist': not is_director,
                'is_partner': not is_director,
                'profession': row[4],
                'phone': row[5], 'tg_username': row[6],
            }
    return None


def _require_director(request):
    """Проверяет, что пользователь — директор. Возвращает (user, error_response)."""
    user = _get_current_user(request)
    if not user:
        from django.shortcuts import redirect
        return None, redirect('/login/')
    if not user.get('is_director'):
        return None, render(request, 'bot_app/login.html', {'error': 'Доступ только для директора.'})
    return user, None


def _require_specialist(request):
    """Проверяет, что пользователь — специалист. Возвращает (user, error_response)."""
    user = _get_current_user(request)
    if not user:
        from django.shortcuts import redirect
        return None, redirect('/login/')
    return user, None


def _dictfetchall(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _dictfetchone(cursor):
    columns = [col[0] for col in cursor.description]
    row = cursor.fetchone()
    return dict(zip(columns, row)) if row else None


def _resolve_partner_tier_bonus(cursor, rating):
    """Возвращает (tier_name, bonus_pct) для партнёра по текущему рейтингу."""
    cursor.execute("""
        SELECT tier_name, bonus_pct
        FROM rating_tiers
        WHERE target_type = 'partner'
          AND is_active = 1
          AND min_rating <= %s
        ORDER BY min_rating DESC
        LIMIT 1
    """, [int(rating)])
    row = _dictfetchone(cursor)
    if not row:
        return None, 0.0
    return row.get('tier_name'), float(row.get('bonus_pct') or 0)


def _get_unread_notif_count():
    try:
        with connection.cursor() as c:
            c.execute("SELECT COUNT(*) FROM notifications WHERE is_read = 0")
            return c.fetchone()[0]
    except Exception:
        return 0


def _column_exists(cursor, table_name, column_name):
    """Проверяет наличие колонки в текущей MySQL-схеме."""
    cursor.execute("""
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
    """, [table_name, column_name])
    return bool((cursor.fetchone() or [0])[0])


def _support_chat_payment_amount_expr(cursor, alias='scp'):
    """
    Возвращает SQL-выражение суммы выплаты:
    - salary_base_amount_rub (если колонка существует),
    - иначе amount_rub (для старых БД без миграции).
    """
    if _column_exists(cursor, 'support_chat_payments', 'salary_base_amount_rub'):
        return f"{alias}.salary_base_amount_rub"
    return f"{alias}.amount_rub"


def _specialist_orders_scope_sql(order_alias='o', payment_alias='scp'):
    """Канонический scope оплаты специалиста: прямой specialist_id + fallback через payment_id."""
    return (
        f"({order_alias}.specialist_id = %s "
        f"OR EXISTS (SELECT 1 FROM support_chat_payments {payment_alias} "
        f"WHERE {payment_alias}.payment_id = {order_alias}.payment_id AND {payment_alias}.specialist_id = %s))"
    )


def _load_tier_name_options():
    """Возвращает уровни из БД отдельно для клиентов и партнёров."""
    defaults = {
        'client': [],
        'partner': ['standard', 'advanced', 'expert', 'top'],
    }
    with connection.cursor() as c:
        c.execute("""
            SELECT target_type, tier_name
            FROM rating_tiers
            WHERE is_active = 1
            ORDER BY target_type, min_rating, tier_name
        """)
        rows = _dictfetchall(c)

    grouped = {'client': [], 'partner': []}
    for row in rows:
        target = (row.get('target_type') or '').strip()
        tier_name = (row.get('tier_name') or '').strip()
        if target in grouped and tier_name and tier_name not in grouped[target]:
            grouped[target].append(tier_name)

    for target, fallback in defaults.items():
        if not grouped[target]:
            grouped[target] = fallback[:]
    return grouped


# ==================== D.2: Кабинет директора ====================

# --- Нормативы лояльности ---

def loyalty_policies_page(request):
    """CRUD для bonus_product_policies и rating_tiers."""
    user, err = _require_director(request)
    if err:
        return err

    if request.method == 'POST':
        action = request.POST.get('action', '')
        try:
            if action == 'save_policy':
                _save_bonus_policy(request, user)
            elif action == 'save_tier':
                _save_rating_tier(request, user)
            elif action == 'delete_policy':
                _delete_by_id('bonus_product_policies', request.POST.get('id'))
            elif action == 'delete_tier':
                _delete_by_id('rating_tiers', request.POST.get('id'))
            return redirect(request.path + '?saved=1')
        except Exception as e:
            logger.exception("loyalty_policies save error")
            tier_options = _load_tier_name_options()
            return render(request, 'bot_app/loyalty_policies.html', {
                'user': user, 'error': str(e),
                **_load_policies_data(),
                'client_tier_options': tier_options['client'],
                'partner_tier_options': tier_options['partner'],
                'unread_notif_count': _get_unread_notif_count(),
            })

    tier_options = _load_tier_name_options()
    return render(request, 'bot_app/loyalty_policies.html', {
        'user': user,
        'success': request.GET.get('saved') == '1',
        **_load_policies_data(),
        'client_tier_options': tier_options['client'],
        'partner_tier_options': tier_options['partner'],
        'unread_notif_count': _get_unread_notif_count(),
    })


def _load_policies_data():
    with connection.cursor() as c:
        c.execute("SELECT * FROM bonus_product_policies ORDER BY product_type, priority DESC")
        policies = _dictfetchall(c)
        c.execute("SELECT * FROM rating_tiers ORDER BY target_type, min_rating")
        tiers = _dictfetchall(c)
    return {'policies': policies, 'tiers': tiers}


def _save_bonus_policy(request, user):
    p = request.POST
    policy_id = p.get('policy_id', '').strip()
    with connection.cursor() as c:
        if policy_id:
            c.execute("""
                UPDATE bonus_product_policies
                SET product_type=%s, event_type=%s, points_per_rub=%s,
                    points_fixed=%s, max_points=%s, min_order_rub=%s,
                    points_lifetime_days=%s, priority=%s, is_active=%s,
                    valid_from=%s, valid_until=%s
                WHERE id=%s
            """, [
                p['product_type'], p['event_type'],
                p.get('points_per_rub') or None, p.get('points_fixed') or None,
                p.get('max_points') or None, p.get('min_order_rub') or None,
                p.get('points_lifetime_days') or None, p.get('priority', 0),
                1 if p.get('is_active') else 0,
                p.get('valid_from') or None, p.get('valid_until') or None,
                int(policy_id),
            ])
        else:
            c.execute("""
                INSERT INTO bonus_product_policies
                (product_type, event_type, points_per_rub, points_fixed,
                 max_points, min_order_rub, points_lifetime_days, priority, is_active,
                 valid_from, valid_until)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, [
                p['product_type'], p['event_type'],
                p.get('points_per_rub') or None, p.get('points_fixed') or None,
                p.get('max_points') or None, p.get('min_order_rub') or None,
                p.get('points_lifetime_days') or None, p.get('priority', 0),
                1 if p.get('is_active') else 0,
                p.get('valid_from') or None, p.get('valid_until') or None,
            ])


def _save_rating_tier(request, user):
    p = request.POST
    tier_id = p.get('tier_id', '').strip()
    with connection.cursor() as c:
        if tier_id:
            c.execute("""
                UPDATE rating_tiers
                SET target_type=%s, tier_name=%s, min_rating=%s,
                    bonus_pct=%s, description=%s, is_active=%s
                WHERE id=%s
            """, [
                p['target_type'], p['tier_name'], int(p['min_rating']),
                p.get('bonus_pct') or 0, p.get('description', ''),
                1 if p.get('is_active') else 0, int(tier_id),
            ])
        else:
            c.execute("""
                INSERT INTO rating_tiers
                (target_type, tier_name, min_rating, bonus_pct, description, is_active)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, [
                p['target_type'], p['tier_name'], int(p['min_rating']),
                p.get('bonus_pct') or 0, p.get('description', ''),
                1 if p.get('is_active') else 0,
            ])


def _delete_by_id(table, row_id):
    if not row_id:
        return
    # whitelist tables
    allowed = {'bonus_product_policies', 'rating_tiers'}
    if table not in allowed:
        return
    with connection.cursor() as c:
        c.execute(f"DELETE FROM {table} WHERE id = %s", [int(row_id)])


# --- Финансы партнёров ---

def partner_finance_page(request):
    """Рейтинг, ставка, зарплата партнёров."""
    user, err = _require_director(request)
    if err:
        return err

    period = request.GET.get('period', datetime.now().strftime('%Y-%m'))

    with connection.cursor() as c:
        # Список партнёров с финансовыми профилями
        c.execute("""
            SELECT s.id, s.name, s.profession, s.phone,
                   pfp.base_revenue_pct, pfp.override_revenue_pct,
                   pfp.rating, pfp.rating_tier,
                   psp.gross_revenue_rub, psp.effective_pct,
                   COALESCE(psp.effective_pct, pfp.base_revenue_pct, 50) - COALESCE(pfp.base_revenue_pct, 50) AS tier_bonus_pct,
                   psp.base_salary_rub, psp.adjustments_rub,
                   psp.final_salary_rub, psp.status AS period_status,
                   psp.id AS period_id
            FROM specialists s
            LEFT JOIN partner_financial_profile pfp ON pfp.specialist_id = s.id
            LEFT JOIN partner_salary_periods psp ON psp.specialist_id = s.id
                  AND psp.period_month = %s
            ORDER BY s.name
        """, [period])
        partners = _dictfetchall(c)

    return render(request, 'bot_app/partner_finance.html', {
        'user': user,
        'partners': partners,
        'period': period,
        'success': request.GET.get('saved') == '1',
        'unread_notif_count': _get_unread_notif_count(),
    })


@require_POST
def api_partner_set_rate(request):
    """API: установить ставку партнёра."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    specialist_id = request.POST.get('specialist_id')
    percent = request.POST.get('percent')
    comment = request.POST.get('comment', '')

    if not specialist_id or not percent:
        return JsonResponse({'error': 'missing params'}, status=400)

    with connection.cursor() as c:
        c.execute("""
            INSERT INTO partner_financial_profile (specialist_id, base_revenue_pct, rating)
            VALUES (%s, %s, 100)
            ON DUPLICATE KEY UPDATE base_revenue_pct = VALUES(base_revenue_pct)
        """, [int(specialist_id), float(percent)])

    return JsonResponse({'ok': True})


@require_POST
def api_partner_complaint(request):
    """API: денежный штраф партнёра (корректировка зарплаты)."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    specialist_id = request.POST.get('specialist_id')
    comment = request.POST.get('comment', '')
    penalty_amount = float(request.POST.get('penalty_amount', 0) or 0)
    period = request.POST.get('period')

    if not specialist_id or not period:
        return JsonResponse({'error': 'missing params'}, status=400)
    if penalty_amount <= 0:
        return JsonResponse({'error': 'penalty_amount must be positive'}, status=400)

    specialist_id_int = int(specialist_id)
    salary_period_id = _ensure_salary_period(specialist_id_int, period)
    with connection.cursor() as c:
        c.execute("""
            INSERT INTO partner_salary_adjustments
            (salary_period_id, type, amount_rub, comment, created_by)
            VALUES (%s, 'penalty', %s, %s, %s)
        """, [salary_period_id, -abs(penalty_amount), comment or 'Денежный штраф', user['id']])
    _recalculate_salary_period_amounts(specialist_id_int, period)

    return JsonResponse({'ok': True})


@require_POST
def api_partner_reward(request):
    """API: денежное вознаграждение партнёра (доплата)."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    specialist_id = request.POST.get('specialist_id')
    comment = request.POST.get('comment', '')
    reward_amount = float(request.POST.get('reward_amount', 0) or 0)
    period = request.POST.get('period')

    if not specialist_id or not period:
        return JsonResponse({'error': 'missing params'}, status=400)
    if reward_amount <= 0:
        return JsonResponse({'error': 'reward_amount must be positive'}, status=400)

    specialist_id_int = int(specialist_id)
    salary_period_id = _ensure_salary_period(specialist_id_int, period)
    with connection.cursor() as c:
        c.execute("""
            INSERT INTO partner_salary_adjustments
            (salary_period_id, type, amount_rub, comment, created_by)
            VALUES (%s, 'bonus', %s, %s, %s)
        """, [salary_period_id, abs(reward_amount), comment or 'Денежное вознаграждение', user['id']])
    _recalculate_salary_period_amounts(specialist_id_int, period)

    return JsonResponse({'ok': True})


@require_POST
def api_partner_adjust_rating(request):
    """API: ручная корректировка рейтинга партнёра (+/-)."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    specialist_id = request.POST.get('specialist_id')
    comment = request.POST.get('comment', '')
    delta = int(request.POST.get('rating_delta', 0) or 0)

    if not specialist_id:
        return JsonResponse({'error': 'missing specialist_id'}, status=400)
    if delta == 0:
        return JsonResponse({'error': 'rating_delta cannot be 0'}, status=400)

    specialist_id_int = int(specialist_id)
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("""
                INSERT INTO partner_financial_profile (specialist_id)
                VALUES (%s)
                ON DUPLICATE KEY UPDATE specialist_id = VALUES(specialist_id)
            """, [specialist_id_int])

            c.execute("""
                SELECT rating
                FROM partner_financial_profile
                WHERE specialist_id = %s
                FOR UPDATE
            """, [specialist_id_int])
            row = _dictfetchone(c)
            rating_before = int(row['rating']) if row else 100
            rating_after = max(0, rating_before + delta)
            tier_name, _ = _resolve_partner_tier_bonus(c, rating_after)

            c.execute("""
                UPDATE partner_financial_profile
                SET rating = %s, rating_tier = COALESCE(%s, rating_tier)
                WHERE specialist_id = %s
            """, [rating_after, tier_name, specialist_id_int])

            c.execute("""
                INSERT INTO partner_rating_events
                (specialist_id, delta, reason, comment, initiated_by, rating_before, rating_after)
                VALUES (%s, %s, 'manual', %s, %s, %s, %s)
            """, [specialist_id_int, delta, comment or 'Ручное изменение рейтинга', user['id'], rating_before, rating_after])

    return JsonResponse({'ok': True})


@require_POST
def api_salary_recalculate(request):
    """API: пересчитать зарплату партнёра за месяц."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    specialist_id = request.POST.get('specialist_id')
    period = request.POST.get('period')

    if not specialist_id or not period:
        return JsonResponse({'error': 'missing params'}, status=400)

    specialist_id_int = int(specialist_id)
    revenue, pct, base_salary, uplift_amount = _upsert_salary_period_base(specialist_id_int, period)
    adj, final_salary = _recalculate_salary_period_amounts(specialist_id_int, period)

    return JsonResponse({
        'ok': True,
        'revenue': revenue,
        'pct': pct,
        'base_salary': base_salary,
        'referral_uplift_amount': uplift_amount,
        'adjustments': adj,
        'final_salary': final_salary,
    })


def _upsert_salary_period_base(specialist_id, period):
    """Пересчитывает базовые поля зарплаты (выручка, ставка, база)."""
    with connection.cursor() as c:
        specialist_scope_sql = _specialist_orders_scope_sql('o', 'scp_scope')

        c.execute(f"""
            SELECT
                COALESCE(SUM(
                    o.amount
                ), 0) AS revenue,
                COALESCE(SUM(
                    CASE
                        WHEN rc.owner_note = CONCAT('specialist:', CAST(%s AS CHAR))
                        THEN o.amount
                        ELSE 0
                    END
                ), 0) AS referred_revenue
            FROM orders o
            LEFT JOIN referral_activations ra ON ra.user_id = CAST(o.user_id AS CHAR)
            LEFT JOIN referral_codes rc ON rc.id = ra.referral_code_id
            WHERE o.status = 'CONFIRMED'
              AND o.paid_at IS NOT NULL
              AND DATE_FORMAT(o.paid_at, '%%Y-%%m') = %s
              AND {specialist_scope_sql}
        """, [specialist_id, period, specialist_id, specialist_id])
        revenue_row = _dictfetchone(c) or {}
        revenue = float(revenue_row.get('revenue') or 0)
        referred_revenue = float(revenue_row.get('referred_revenue') or 0)

        c.execute("""
            SELECT COALESCE(number_value, 0) AS uplift_pct
            FROM system_settings
            WHERE name = 'LOYALTY_PARTNER_REFERRAL_UPLIFT_PCT'
            LIMIT 1
        """)
        uplift_row = _dictfetchone(c) or {}
        uplift_pct = float(uplift_row.get('uplift_pct') or 0)

        c.execute("""
            SELECT COALESCE(override_revenue_pct, base_revenue_pct, 50) AS base_pct,
                   COALESCE(rating, 100) AS rating
            FROM partner_financial_profile
            WHERE specialist_id = %s
        """, [specialist_id])
        row = _dictfetchone(c)
        base_pct = float(row['base_pct']) if row else 50.0
        rating = int(row['rating']) if row else 100
        tier_name, tier_bonus_pct = _resolve_partner_tier_bonus(c, rating)
        pct = base_pct + tier_bonus_pct
        referral_uplift_amount = referred_revenue * uplift_pct / 100
        base_salary = (revenue * pct / 100) + referral_uplift_amount

        if row:
            c.execute("""
                UPDATE partner_financial_profile
                SET rating_tier = COALESCE(%s, rating_tier)
                WHERE specialist_id = %s
            """, [tier_name, specialist_id])

        c.execute("""
            INSERT INTO partner_salary_periods
            (specialist_id, period_month, gross_revenue_rub, effective_pct,
             base_salary_rub, adjustments_rub, final_salary_rub, status)
            VALUES (%s,%s,%s,%s,%s,0,%s,'draft')
            ON DUPLICATE KEY UPDATE
              gross_revenue_rub = VALUES(gross_revenue_rub),
              effective_pct = VALUES(effective_pct),
              base_salary_rub = VALUES(base_salary_rub)
        """, [specialist_id, period, revenue, pct, base_salary, base_salary])
    return revenue, pct, base_salary, referral_uplift_amount


def _ensure_salary_period(specialist_id, period):
    """Гарантирует наличие строки периода зарплаты партнёра."""
    _upsert_salary_period_base(specialist_id, period)
    with connection.cursor() as c:
        c.execute("""
            SELECT id
            FROM partner_salary_periods
            WHERE specialist_id = %s AND period_month = %s
            LIMIT 1
        """, [specialist_id, period])
        row = _dictfetchone(c)
        return int(row['id'])


def _recalculate_salary_period_amounts(specialist_id, period):
    """Пересчитывает корректировки и итоговую выплату."""
    with connection.cursor() as c:
        c.execute("""
            SELECT psp.id, psp.base_salary_rub
            FROM partner_salary_periods psp
            WHERE psp.specialist_id = %s AND psp.period_month = %s
            LIMIT 1
        """, [specialist_id, period])
        row = _dictfetchone(c)
        if not row:
            _, _, _, _ = _upsert_salary_period_base(specialist_id, period)
            return _recalculate_salary_period_amounts(specialist_id, period)

        salary_period_id = int(row['id'])
        base_salary = float(row['base_salary_rub'] or 0)

        c.execute("""
            SELECT COALESCE(SUM(amount_rub), 0) AS adj
            FROM partner_salary_adjustments
            WHERE salary_period_id = %s
        """, [salary_period_id])
        adj = float(_dictfetchone(c)['adj'])
        final_salary = base_salary + adj

        c.execute("""
            UPDATE partner_salary_periods
            SET adjustments_rub = %s, final_salary_rub = %s
            WHERE id = %s
        """, [adj, final_salary, salary_period_id])

    return adj, final_salary


# --- Журнал бонусных операций ---

def loyalty_journal_page(request):
    """Журнал loyalty_transactions с фильтрацией."""
    user, err = _require_director(request)
    if err:
        return err

    page = int(request.GET.get('page', 1))
    per_page = 50
    offset = (page - 1) * per_page
    event_filter = request.GET.get('event_type', '')
    direction_filter = request.GET.get('direction', '')

    where_parts = ["1=1"]
    params = []

    if event_filter:
        where_parts.append("lt.event_type = %s")
        params.append(event_filter)
    if direction_filter:
        where_parts.append("lt.direction = %s")
        params.append(direction_filter)

    where_sql = " AND ".join(where_parts)

    with connection.cursor() as c:
        c.execute(f"""
            SELECT lt.*, la.owner_type, la.owner_id,
                   u.first_name, u.last_name, u.inn, u.phone,
                   s.name AS specialist_name
            FROM loyalty_transactions lt
            JOIN loyalty_accounts la ON la.id = lt.account_id
            LEFT JOIN users u ON u.id = la.owner_id AND la.owner_type = 'client'
            LEFT JOIN specialists s ON s.id = la.owner_id AND la.owner_type = 'specialist'
            WHERE {where_sql}
            ORDER BY lt.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        transactions = _dictfetchall(c)

        c.execute(f"""
            SELECT COUNT(*) AS cnt
            FROM loyalty_transactions lt
            WHERE {where_sql}
        """, params)
        total = _dictfetchone(c)['cnt']

    total_pages = (total + per_page - 1) // per_page

    return render(request, 'bot_app/loyalty_journal.html', {
        'user': user,
        'transactions': transactions,
        'page': page,
        'total_pages': total_pages,
        'total': total,
        'event_filter': event_filter,
        'direction_filter': direction_filter,
        'unread_notif_count': _get_unread_notif_count(),
    })


# ==================== D.3: Кабинет партнёра ====================

def partner_cabinet_page(request):
    """Кабинет партнёра: рейтинг, ставка, выручка и детализация."""
    user, err = _require_specialist(request)
    if err:
        return err

    specialist_id = user['id']
    now = datetime.now()
    period = request.GET.get('period', now.strftime('%Y-%m'))

    try:
        p_year, p_month = map(int, period.split('-'))
    except Exception:
        p_year, p_month = now.year, now.month

    with connection.cursor() as c:
        # Финансовый профиль
        c.execute("""
            SELECT * FROM partner_financial_profile
            WHERE specialist_id = %s
        """, [specialist_id])
        profile = _dictfetchone(c)
        if profile:
            resolved_tier_name, _ = _resolve_partner_tier_bonus(c, profile.get('rating') or 0)
            profile['rating_tier'] = resolved_tier_name or 'Без уровня'
        else:
            profile = {'rating_tier': 'Без уровня'}

        # Зарплатный период
        c.execute("""
            SELECT * FROM partner_salary_periods
            WHERE specialist_id = %s AND period_month = %s
        """, [specialist_id, period])
        salary = _dictfetchone(c)

        # Корректировки
        adjustments = []
        if salary:
            c.execute("""
                SELECT * FROM partner_salary_adjustments
                WHERE salary_period_id = %s
                ORDER BY created_at DESC
            """, [salary['id']])
            adjustments = _dictfetchall(c)

        # Рейтинговые события
        c.execute("""
            SELECT pre.created_at,
                   COALESCE(pre.reason, '—') AS event_type,
                   pre.delta,
                   pre.comment,
                   sp.name AS initiated_by_name
            FROM partner_rating_events pre
            LEFT JOIN specialists sp ON sp.id = pre.initiated_by
            WHERE pre.specialist_id = %s
            ORDER BY pre.created_at DESC
            LIMIT 20
        """, [specialist_id])
        rating_events = _dictfetchall(c)

        # Текущая выручка за период (канонически из orders с fallback по payment_id)
        specialist_scope_sql = _specialist_orders_scope_sql('o', 'scp_scope')
        c.execute(f"""
            SELECT COUNT(*) AS cnt,
                   COALESCE(SUM(o.amount), 0) AS total
            FROM orders o
            WHERE o.status = 'CONFIRMED'
              AND o.paid_at IS NOT NULL
              AND DATE_FORMAT(o.paid_at, '%%Y-%%m') = %s
              AND {specialist_scope_sql}
        """, [period, specialist_id, specialist_id])
        current_stats = _dictfetchone(c)

        # --- ЭТАП 5: Расширенная детализация ---

        # Детализация по клиентам
        c.execute("""
            SELECT u.id, u.first_name, u.last_name, u.inn,
                   COUNT(*) AS consult_count,
                   COALESCE(SUM(scp.amount_rub), 0) AS revenue
            FROM support_chat_payments scp
            JOIN support_chats sch ON sch.id = scp.chat_id
            JOIN users u ON u.id = sch.client_user_id
            WHERE scp.specialist_id = %s AND scp.status = 'CONFIRMED'
              AND YEAR(scp.created_at) = %s AND MONTH(scp.created_at) = %s
            GROUP BY u.id, u.first_name, u.last_name, u.inn
            ORDER BY revenue DESC
        """, [specialist_id, p_year, p_month])
        clients_detail = _dictfetchall(c)

        # Детализация по услугам
        c.execute("""
            SELECT COALESCE(NULLIF(sch.request_type, ''), 'Консультация') AS service_type,
                   COUNT(*) AS cnt,
                   COALESCE(AVG(scp.amount_rub), 0) AS avg_check,
                   COALESCE(SUM(scp.amount_rub), 0) AS total_revenue
            FROM support_chat_payments scp
            LEFT JOIN support_chats sch ON sch.id = scp.chat_id
            WHERE scp.specialist_id = %s AND scp.status = 'CONFIRMED'
              AND YEAR(scp.created_at) = %s AND MONTH(scp.created_at) = %s
            GROUP BY COALESCE(NULLIF(sch.request_type, ''), 'Консультация')
            ORDER BY total_revenue DESC
        """, [specialist_id, p_year, p_month])
        services_detail = _dictfetchall(c)

    return render(request, 'bot_app/partner_cabinet.html', {
        'user': user,
        'profile': profile or {},
        'salary': salary or {},
        'adjustments': adjustments,
        'rating_events': rating_events,
        'current_stats': current_stats or {},
        'period': period,
        'clients_detail': clients_detail,
        'services_detail': services_detail,
        'unread_notif_count': _get_unread_notif_count(),
    })


# ==================== E.2: Регламент закрытия месяца ====================

@require_POST
def api_salary_approve(request):
    """Перевести зарплатный период из draft → approved."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    period_id = request.POST.get('period_id')
    if not period_id:
        return JsonResponse({'error': 'missing period_id'}, status=400)

    with connection.cursor() as c:
        c.execute("""
            UPDATE partner_salary_periods
            SET status = 'approved', approved_by = %s, approved_at = NOW()
            WHERE id = %s AND status = 'draft'
        """, [user['id'], int(period_id)])
        affected = c.rowcount

    if affected:
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'not_found_or_wrong_status'}, status=400)


@require_POST
def api_salary_mark_paid(request):
    """Перевести зарплатный период из approved → paid."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    period_id = request.POST.get('period_id')
    if not period_id:
        return JsonResponse({'error': 'missing period_id'}, status=400)

    with connection.cursor() as c:
        c.execute("""
            UPDATE partner_salary_periods
            SET status = 'paid', paid_at = NOW()
            WHERE id = %s AND status = 'approved'
        """, [int(period_id)])
        affected = c.rowcount

    if affected:
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'not_found_or_wrong_status'}, status=400)


@require_POST
def api_salary_approve_all(request):
    """Перевести все draft → approved за период."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    period = request.POST.get('period')
    if not period:
        return JsonResponse({'error': 'missing period'}, status=400)

    with connection.cursor() as c:
        c.execute("""
            UPDATE partner_salary_periods
            SET status = 'approved', approved_by = %s, approved_at = NOW()
            WHERE period_month = %s AND status = 'draft'
        """, [user['id'], period])
        affected = c.rowcount

    return JsonResponse({'ok': True, 'approved_count': affected})


# ==================== Финансовый дашборд ====================

def financial_dashboard_page(request):
    """Главный финансовый дашборд директора."""
    user, err = _require_director(request)
    if err:
        return err

    now = datetime.now()
    period = request.GET.get('period', now.strftime('%Y-%m'))
    # Разбираем период
    try:
        p_year, p_month = map(int, period.split('-'))
    except Exception:
        p_year, p_month = now.year, now.month

    with connection.cursor() as c:
        # === KPI-карточки ===

        # 1. Активных клиентов с баллами
        c.execute("""
            SELECT COUNT(*) FROM loyalty_balances lb
            JOIN loyalty_accounts la ON la.id = lb.account_id
            WHERE la.owner_type = 'client' AND lb.available_amount > 0
        """)
        active_clients = c.fetchone()[0] or 0

        # 2. Общий баланс баллов
        c.execute("""
            SELECT COALESCE(SUM(lb.available_amount), 0),
                   COALESCE(SUM(lb.reserved_amount), 0)
            FROM loyalty_balances lb
            JOIN loyalty_accounts la ON la.id = lb.account_id
            WHERE la.owner_type = 'client'
        """)
        row = c.fetchone()
        total_balance = row[0] or 0
        total_reserved = row[1] or 0

        # 3. Средний баланс
        avg_balance = round(total_balance / active_clients, 1) if active_clients else 0

        # 4. Начислено/списано за период
        c.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END), 0) AS accrued,
                COALESCE(SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END), 0) AS spent
            FROM loyalty_transactions
            WHERE YEAR(created_at) = %s AND MONTH(created_at) = %s
        """, [p_year, p_month])
        tx_row = c.fetchone()
        accrued_month = tx_row[0] or 0
        spent_month = tx_row[1] or 0

        # 5. Фонд зарплаты партнёров за период
        c.execute("""
            SELECT COALESCE(SUM(final_salary_rub), 0)
            FROM partner_salary_periods
            WHERE period_month = %s
        """, [period])
        salary_fund = c.fetchone()[0] or 0

        # 6. Всего клиентов в системе
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0] or 0

        # === Распределение по tier ===
        c.execute("""
            SELECT COALESCE(tier, 'none') AS tier, COUNT(*) AS cnt
            FROM user_loyalty_profile
            GROUP BY tier ORDER BY cnt DESC
        """)
        tier_distribution = _dictfetchall(c)

        # === Топ-10 клиентов по балансу ===
        c.execute("""
            SELECT u.id, COALESCE(u.first_name, '') AS first_name,
                   COALESCE(u.last_name, '') AS last_name,
                   COALESCE(u.phone, '') AS phone,
                   lb.available_amount,
                   COALESCE(ulp.tier, 'none') AS tier,
                   COALESCE(ulp.rating, 0) AS rating
            FROM loyalty_balances lb
            JOIN loyalty_accounts la ON la.id = lb.account_id
            JOIN users u ON u.id = la.owner_id AND la.owner_type = 'client'
            LEFT JOIN user_loyalty_profile ulp ON ulp.user_id = u.id
            ORDER BY lb.available_amount DESC
            LIMIT 10
        """)
        top_clients = _dictfetchall(c)

        # === Топ-5 партнёров по выручке ===
        c.execute("""
            SELECT s.id, s.name,
                   COALESCE(psp.gross_revenue_rub, 0) AS revenue,
                   COALESCE(psp.final_salary_rub, 0) AS salary,
                   COALESCE(pfp.rating_tier, 'standard') AS tier
            FROM specialists s
            JOIN partner_financial_profile pfp ON pfp.specialist_id = s.id
            LEFT JOIN partner_salary_periods psp
                ON psp.specialist_id = s.id AND psp.period_month = %s
            WHERE s.is_active = 1 AND s.is_director = 0
            ORDER BY COALESCE(psp.gross_revenue_rub, 0) DESC
            LIMIT 5
        """, [period])
        top_partners = _dictfetchall(c)

        # === Динамика за 30 дней ===
        c.execute("""
            SELECT DATE(created_at) AS dt,
                   SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) AS accrued,
                   SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) AS spent
            FROM loyalty_transactions
            WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
            GROUP BY DATE(created_at)
            ORDER BY dt
        """)
        timeline_raw = _dictfetchall(c)
        timeline_labels = [r['dt'].strftime('%d.%m') if hasattr(r['dt'], 'strftime') else str(r['dt']) for r in timeline_raw]
        timeline_accrued = [float(r['accrued'] or 0) for r in timeline_raw]
        timeline_spent = [float(r['spent'] or 0) for r in timeline_raw]

        # === Алерты ===
        alerts = []

        # Массовое сгорание баллов за последние 24 часа
        c.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM loyalty_transactions
            WHERE event_type = 'expiry' AND created_at >= DATE_SUB(NOW(), INTERVAL 1 DAY)
        """)
        expired_24h = c.fetchone()[0] or 0
        if expired_24h > 1000:
            alerts.append({
                'type': 'warning',
                'text': f'За последние 24ч сгорело {int(expired_24h)} баллов'
            })

        # Нерассчитанные зарплаты
        c.execute("""
            SELECT COUNT(*) FROM partner_salary_periods
            WHERE period_month = %s AND status = 'draft'
        """, [period])
        draft_salaries = c.fetchone()[0] or 0
        if draft_salaries > 0:
            alerts.append({
                'type': 'info',
                'text': f'{draft_salaries} зарплат в статусе "черновик" за {period}'
            })

        # Последние операции
        c.execute("""
            SELECT lt.id, lt.direction, lt.amount, lt.event_type,
                   lt.description, lt.created_at,
                   la.owner_type, la.owner_id
            FROM loyalty_transactions lt
            JOIN loyalty_accounts la ON la.id = lt.account_id
            ORDER BY lt.created_at DESC
            LIMIT 15
        """)
        recent_transactions = _dictfetchall(c)

    ctx = {
        'user': user,
        'period': period,
        'active_clients': active_clients,
        'total_balance': total_balance,
        'total_reserved': total_reserved,
        'avg_balance': avg_balance,
        'accrued_month': accrued_month,
        'spent_month': spent_month,
        'salary_fund': salary_fund,
        'total_users': total_users,
        'tier_distribution': json.dumps(tier_distribution),
        'top_clients': top_clients,
        'top_partners': top_partners,
        'timeline_labels': json.dumps(timeline_labels),
        'timeline_accrued': json.dumps(timeline_accrued),
        'timeline_spent': json.dumps(timeline_spent),
        'alerts': alerts,
        'recent_transactions': recent_transactions,
        'unread_notif_count': _get_unread_notif_count(),
    }
    return render(request, 'bot_app/financial_dashboard.html', ctx)


# ==================== ЭТАП 3: Ручные операции с баллами ====================

def manual_operations_page(request):
    """
    Страница ручных операций с баллами.
    Доступ: только директор.
    """
    user, err = _require_director(request)
    if err:
        return err
    
    # Получить историю ручных операций (последние 50)
    with connection.cursor() as c:
        c.execute("""
            SELECT lt.*, la.owner_type, la.owner_id,
                   u.first_name, u.last_name, u.inn, u.phone
            FROM loyalty_transactions lt
            JOIN loyalty_accounts la ON la.id = lt.account_id
            LEFT JOIN users u ON u.id = la.owner_id AND la.owner_type = 'client'
            WHERE lt.event_type IN ('manual_accrual', 'manual_debit', 'manual_rating_adjustment')
            ORDER BY lt.created_at DESC
            LIMIT 50
        """)
        history = _dictfetchall(c)
    
    return render(request, 'bot_app/manual_operations.html', {
        'user': user,
        'history': history,
        'unread_notif_count': _get_unread_notif_count(),
    })


@require_POST
def api_manual_accrual(request):
    """
    Ручное начисление баллов клиенту.
    
    POST params:
    - client_id: int (internal user_id)
    - amount: int (баллы)
    - reason: str (причина)
    - comment: str (опционально)
    - lifetime_days: int (опционально, по умолчанию из настроек)
    """
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)
    
    try:
        client_id = int(request.POST.get('client_id'))
        amount = int(request.POST.get('amount'))
        reason = request.POST.get('reason', '').strip()
        comment = request.POST.get('comment', '').strip()
        lifetime_days = request.POST.get('lifetime_days')
        
        if amount <= 0:
            return JsonResponse({'error': 'amount_must_be_positive'}, status=400)
        if not reason:
            return JsonResponse({'error': 'reason_required'}, status=400)
        
        # Проверить существование клиента
        with connection.cursor() as c:
            c.execute("SELECT id, first_name, last_name FROM users WHERE id = %s", [client_id])
            client = _dictfetchone(c)
            if not client:
                return JsonResponse({'error': 'client_not_found'}, status=404)
        
        # Вызвать ledger (синхронная обёртка)
        import asyncio
        from services.ledger import post_accrual
        
        meta = {
            'initiated_by': user['id'],
            'initiated_by_name': user['name'],
            'reason': reason,
            'comment': comment,
        }
        
        result = asyncio.run(post_accrual(
            owner_type='client',
            owner_id=client_id,
            amount=amount,
            event_type='manual_accrual',
            idempotency_key=f"manual_accrual:{client_id}:{datetime.utcnow().isoformat()}:{user['id']}",
            description=f"Ручное начисление: {reason}",
            rating_delta=0,
            lifetime_days=int(lifetime_days) if lifetime_days else None,
            meta=meta,
        ))
        
        if result.get('ok'):
            logger.info(f"Manual accrual: {amount} points to client {client_id} by {user['id']}")
            return JsonResponse({
                'ok': True,
                'transaction_id': result['transaction']['id'],
                'new_balance': result.get('new_balance', 0),
            })
        else:
            return JsonResponse({'error': result.get('error', 'unknown')}, status=400)
            
    except ValueError as e:
        return JsonResponse({'error': f'invalid_input: {e}'}, status=400)
    except Exception as e:
        logger.exception("Manual accrual failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


@require_POST
def api_manual_debit(request):
    """
    Ручное списание баллов у клиента.
    
    POST params:
    - client_id: int
    - amount: int
    - reason: str
    - comment: str (опционально)
    """
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)
    
    try:
        client_id = int(request.POST.get('client_id'))
        amount = int(request.POST.get('amount'))
        reason = request.POST.get('reason', '').strip()
        comment = request.POST.get('comment', '').strip()
        
        if amount <= 0:
            return JsonResponse({'error': 'amount_must_be_positive'}, status=400)
        if not reason:
            return JsonResponse({'error': 'reason_required'}, status=400)
        
        # Проверить клиента
        with connection.cursor() as c:
            c.execute("SELECT id FROM users WHERE id = %s", [client_id])
            if not c.fetchone():
                return JsonResponse({'error': 'client_not_found'}, status=404)
        
        # Резервировать + списать
        import asyncio
        from services.ledger import reserve_points, redeem_reserved_points
        
        reservation_key = f"manual_debit:{client_id}:{datetime.utcnow().isoformat()}:{user['id']}"
        
        meta = {
            'initiated_by': user['id'],
            'initiated_by_name': user['name'],
            'reason': reason,
            'comment': comment,
        }
        
        # Резерв
        reserve_result = asyncio.run(reserve_points(
            owner_type='client',
            owner_id=client_id,
            amount=amount,
            idempotency_key=reservation_key,
            description=f"Ручное списание: {reason}",
            ref_entity_type='manual_debit',
            meta=meta,
        ))
        
        if not reserve_result.get('ok'):
            error = reserve_result.get('error', 'unknown')
            if error == 'insufficient_balance':
                available = reserve_result.get('available', 0)
                logger.warning(f"Manual debit failed: insufficient balance for client {client_id}")
                return JsonResponse({
                    'error': 'insufficient_balance',
                    'available': available,
                    'requested': amount,
                }, status=400)
            return JsonResponse({'error': error}, status=400)
        
        # Подтвердить списание
        redeem_result = asyncio.run(redeem_reserved_points(reservation_key))
        
        if redeem_result.get('ok'):
            logger.info(f"Manual debit: {amount} points from client {client_id} by {user['id']}")
            return JsonResponse({
                'ok': True,
                'transaction_id': redeem_result['transaction']['id'],
            })
        else:
            return JsonResponse({'error': redeem_result.get('error', 'unknown')}, status=500)
            
    except ValueError as e:
        return JsonResponse({'error': f'invalid_input: {e}'}, status=400)
    except Exception as e:
        logger.exception("Manual debit failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


@require_POST
def api_manual_adjust_rating(request):
    """
    Ручная корректировка рейтинга клиента.
    
    POST params:
    - client_id: int
    - rating_delta: int (может быть отрицательным)
    - reason: str
    - comment: str (опционально)
    """
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)
    
    try:
        client_id = int(request.POST.get('client_id'))
        rating_delta = int(request.POST.get('rating_delta'))
        reason = request.POST.get('reason', '').strip()
        comment = request.POST.get('comment', '').strip()
        
        if rating_delta == 0:
            return JsonResponse({'error': 'rating_delta_cannot_be_zero'}, status=400)
        if not reason:
            return JsonResponse({'error': 'reason_required'}, status=400)
        
        # Проверить клиента
        with connection.cursor() as c:
            c.execute("SELECT id FROM users WHERE id = %s", [client_id])
            if not c.fetchone():
                return JsonResponse({'error': 'client_not_found'}, status=404)
        
        # Получить текущий профиль
        import asyncio
        from db.loyalty import get_user_loyalty_profile, update_user_loyalty_profile
        from services.loyalty_engine import _maybe_update_tier
        
        profile = asyncio.run(get_user_loyalty_profile(client_id))
        if not profile:
            return JsonResponse({'error': 'profile_not_found'}, status=404)
        
        rating_before = profile['rating']
        rating_after = rating_before + rating_delta
        tier_before = profile['tier']
        
        # Обновить рейтинг
        asyncio.run(update_user_loyalty_profile(
            user_id=client_id,
            rating_delta=rating_delta,
            touch_activity=True,
        ))
        
        # Пересчитать tier
        new_tier = asyncio.run(_maybe_update_tier(client_id))
        
        # Создать транзакцию для audit trail
        from services.ledger import post_accrual
        
        meta = {
            'initiated_by': user['id'],
            'initiated_by_name': user['name'],
            'reason': reason,
            'comment': comment,
            'rating_before': rating_before,
            'rating_after': rating_after,
            'rating_delta': rating_delta,
            'tier_before': tier_before,
            'tier_after': new_tier,
        }
        
        # Создать транзакцию с нулевым amount (только для audit)
        asyncio.run(post_accrual(
            owner_type='client',
            owner_id=client_id,
            amount=0,
            event_type='manual_rating_adjustment',
            idempotency_key=f"rating_adj:{client_id}:{datetime.utcnow().isoformat()}:{user['id']}",
            description=f"Корректировка рейтинга: {reason}",
            rating_delta=rating_delta,
            meta=meta,
        ))
        
        logger.info(f"Manual rating adjustment: {rating_delta} for client {client_id} by {user['id']}")
        return JsonResponse({
            'ok': True,
            'rating_before': rating_before,
            'rating_after': rating_after,
            'rating_delta': rating_delta,
            'tier_before': tier_before,
            'tier_after': new_tier,
        })
        
    except ValueError as e:
        return JsonResponse({'error': f'invalid_input: {e}'}, status=400)
    except Exception as e:
        logger.exception("Manual rating adjustment failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


def api_search_client(request):
    """
    Поиск клиентов по ИНН, телефону или имени.
    
    GET params:
    - q: str (поисковый запрос)
    
    Returns: список клиентов с балансом и рейтингом
    """
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)
    
    query = request.GET.get('q', '').strip()
    if len(query) < 3:
        return JsonResponse({'results': []})
    
    try:
        import asyncio
        from db.clients import search_clients_by_inn_phone_name
        from db.loyalty import get_loyalty_balance
        
        # Поиск клиентов
        clients = asyncio.run(search_clients_by_inn_phone_name(query, limit=10))
        
        # Обогатить данными о балансе и рейтинге
        results = []
        for client in clients:
            balance_info = asyncio.run(get_loyalty_balance('client', client['id']))
            
            results.append({
                'id': client['id'],
                'name': f"{client.get('first_name', '')} {client.get('last_name', '')}".strip(),
                'inn': client.get('inn', ''),
                'phone': client.get('phone', ''),
                'balance': balance_info.get('available_amount', 0) if balance_info else 0,
                'rating': client.get('rating', 0),
                'tier': client.get('tier', 'none'),
            })
        
        return JsonResponse({'results': results})
        
    except Exception as e:
        logger.exception("Client search failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


def api_manual_history(request):
    """
    История ручных операций.
    
    GET params:
    - event_type: str (опционально, фильтр по типу)
    - limit: int (опционально, по умолчанию 50)
    """
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)
    
    event_type = request.GET.get('event_type', '').strip()
    limit = int(request.GET.get('limit', 50))
    limit = min(limit, 200)
    
    try:
        with connection.cursor() as c:
            if event_type and event_type != 'all':
                c.execute("""
                    SELECT lt.*, la.owner_type, la.owner_id,
                           u.first_name, u.last_name, u.inn, u.phone
                    FROM loyalty_transactions lt
                    JOIN loyalty_accounts la ON la.id = lt.account_id
                    LEFT JOIN users u ON u.id = la.owner_id AND la.owner_type = 'client'
                    WHERE lt.event_type = %s
                    ORDER BY lt.created_at DESC
                    LIMIT %s
                """, [event_type, limit])
            else:
                c.execute("""
                    SELECT lt.*, la.owner_type, la.owner_id,
                           u.first_name, u.last_name, u.inn, u.phone
                    FROM loyalty_transactions lt
                    JOIN loyalty_accounts la ON la.id = lt.account_id
                    LEFT JOIN users u ON u.id = la.owner_id AND la.owner_type = 'client'
                    WHERE lt.event_type IN ('manual_accrual', 'manual_debit', 'manual_rating_adjustment')
                    ORDER BY lt.created_at DESC
                    LIMIT %s
                """, [limit])
            
            history = _dictfetchall(c)
        
        # Преобразовать в JSON-friendly формат
        for item in history:
            if item.get('created_at'):
                item['created_at'] = item['created_at'].isoformat()
            if item.get('meta_json'):
                item['meta'] = json.loads(item['meta_json'])
        
        return JsonResponse({'history': history})
        
    except Exception as e:
        logger.exception("History fetch failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


# ==================== ЭТАП 5: Аналитика лояльности ====================

def loyalty_analytics_page(request):
    """Страница аналитики лояльности — 4 вкладки."""
    user, err = _require_director(request)
    if err:
        return err

    now = datetime.now()
    period = request.GET.get('period', now.strftime('%Y-%m'))
    try:
        p_year, p_month = map(int, period.split('-'))
    except Exception:
        p_year, p_month = now.year, now.month

    with connection.cursor() as c:
        payment_amount_expr = _support_chat_payment_amount_expr(c, alias='scp')

        # ---------- Tab 1: По клиентам (summary) ----------
        c.execute("""
            SELECT COUNT(*) FROM user_loyalty_profile
        """)
        total_profiles = c.fetchone()[0] or 0

        c.execute("""
            SELECT COALESCE(tier, 'none') AS tier, COUNT(*) AS cnt
            FROM user_loyalty_profile
            GROUP BY tier ORDER BY cnt DESC
        """)
        tier_distribution = _dictfetchall(c)

        # ---------- Tab 2: По операциям (summary) ----------
        c.execute("""
            SELECT event_type,
                   COUNT(*) AS tx_count,
                   COALESCE(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END), 0) AS total_credit,
                   COALESCE(SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END), 0) AS total_debit
            FROM loyalty_transactions
            WHERE YEAR(created_at) = %s AND MONTH(created_at) = %s
            GROUP BY event_type
            ORDER BY tx_count DESC
        """, [p_year, p_month])
        operations_by_type = _dictfetchall(c)

        # Timeline по дням за выбранный месяц
        c.execute("""
            SELECT DATE(created_at) AS dt,
                   SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) AS accrued,
                   SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) AS spent,
                   COUNT(*) AS tx_count
            FROM loyalty_transactions
            WHERE YEAR(created_at) = %s AND MONTH(created_at) = %s
            GROUP BY DATE(created_at)
            ORDER BY dt
        """, [p_year, p_month])
        timeline_raw = _dictfetchall(c)

        timeline_labels = [r['dt'].strftime('%d.%m') if hasattr(r['dt'], 'strftime') else str(r['dt']) for r in timeline_raw]
        timeline_accrued = [float(r['accrued'] or 0) for r in timeline_raw]
        timeline_spent = [float(r['spent'] or 0) for r in timeline_raw]
        timeline_counts = [int(r['tx_count'] or 0) for r in timeline_raw]

        # ---------- Tab 3: Реферальная программа ----------
        c.execute("""
            SELECT COUNT(*) FROM loyalty_transactions
            WHERE event_type = 'referral_bonus'
              AND YEAR(created_at) = %s AND MONTH(created_at) = %s
        """, [p_year, p_month])
        referral_activations = c.fetchone()[0] or 0

        c.execute("""
            SELECT COALESCE(number_value, 0)
            FROM system_settings
            WHERE name = 'LOYALTY_PARTNER_REFERRAL_UPLIFT_PCT'
            LIMIT 1
        """)
        uplift_row = c.fetchone()
        referral_uplift_pct = float((uplift_row[0] if uplift_row else 0) or 0)

        c.execute("""
            SELECT la.owner_id,
                   u.first_name, u.last_name, u.inn,
                   COUNT(*) AS ref_count,
                   COALESCE(SUM(lt.amount), 0) AS total_bonus
            FROM loyalty_transactions lt
            JOIN loyalty_accounts la ON la.id = lt.account_id
            LEFT JOIN users u ON u.id = la.owner_id AND la.owner_type = 'client'
            WHERE lt.event_type = 'referral_bonus'
              AND YEAR(lt.created_at) = %s AND MONTH(lt.created_at) = %s
            GROUP BY la.owner_id, u.first_name, u.last_name, u.inn
            ORDER BY ref_count DESC
            LIMIT 10
        """, [p_year, p_month])
        top_referrers = _dictfetchall(c)

        c.execute(f"""
            SELECT
                COALESCE(SUM(
                    ({payment_amount_expr}) * %s / 100
                ), 0) AS extra_income
            FROM support_chat_payments scp
            JOIN support_chats sch ON sch.id = scp.chat_id
            JOIN referral_activations ra
              ON ra.user_id = CAST(sch.client_user_id AS CHAR)
            JOIN referral_codes rc ON rc.id = ra.referral_code_id
            JOIN specialists s ON s.id = scp.specialist_id
            WHERE scp.status = 'CONFIRMED'
              AND YEAR(scp.created_at) = %s AND MONTH(scp.created_at) = %s
              AND rc.owner_note = CONCAT('specialist:', CAST(s.id AS CHAR))
        """, [referral_uplift_pct, p_year, p_month])
        referral_partner_extra_income = float((c.fetchone() or [0])[0] or 0)

        c.execute(f"""
            SELECT
                s.id AS specialist_id,
                s.name,
                COUNT(DISTINCT sch.client_user_id) AS referred_clients,
                COUNT(*) AS paid_count,
                COALESCE(SUM(
                    {payment_amount_expr}
                ), 0) AS referred_revenue,
                COALESCE(SUM(
                    ({payment_amount_expr}) * %s / 100
                ), 0) AS extra_income
            FROM support_chat_payments scp
            JOIN support_chats sch ON sch.id = scp.chat_id
            JOIN referral_activations ra
              ON ra.user_id = CAST(sch.client_user_id AS CHAR)
            JOIN referral_codes rc ON rc.id = ra.referral_code_id
            JOIN specialists s ON s.id = scp.specialist_id
            WHERE scp.status = 'CONFIRMED'
              AND YEAR(scp.created_at) = %s AND MONTH(scp.created_at) = %s
              AND rc.owner_note = CONCAT('specialist:', CAST(s.id AS CHAR))
            GROUP BY s.id, s.name
            ORDER BY extra_income DESC
            LIMIT 10
        """, [referral_uplift_pct, p_year, p_month])
        referral_partner_extra_top = _dictfetchall(c)

        # ---------- Tab 4: По продуктам ----------
        c.execute("""
            SELECT
                COALESCE(JSON_UNQUOTE(JSON_EXTRACT(meta_json, '$.product_type')), 'unknown') AS product_type,
                COUNT(*) AS tx_count,
                COALESCE(SUM(amount), 0) AS total_points,
                COALESCE(AVG(amount), 0) AS avg_points
            FROM loyalty_transactions
            WHERE direction = 'credit'
              AND event_type NOT IN ('manual_accrual', 'manual_rating_adjustment', 'referral_bonus', 'expiry')
              AND YEAR(created_at) = %s AND MONTH(created_at) = %s
            GROUP BY product_type
            ORDER BY total_points DESC
        """, [p_year, p_month])
        products_stats = _dictfetchall(c)

    ctx = {
        'user': user,
        'period': period,
        'total_profiles': total_profiles,
        'tier_distribution': tier_distribution,
        'tier_distribution_json': json.dumps(tier_distribution, default=str),
        'operations_by_type': operations_by_type,
        'timeline_labels': json.dumps(timeline_labels),
        'timeline_accrued': json.dumps(timeline_accrued),
        'timeline_spent': json.dumps(timeline_spent),
        'timeline_counts': json.dumps(timeline_counts),
        'referral_activations': referral_activations,
        'top_referrers': top_referrers,
        'referral_partner_extra_income': referral_partner_extra_income,
        'referral_partner_extra_top': referral_partner_extra_top,
        'products_stats': products_stats,
        'client_tier_options': _load_tier_name_options()['client'],
        'unread_notif_count': _get_unread_notif_count(),
    }
    return render(request, 'bot_app/loyalty_analytics.html', ctx)


def api_analytics_clients(request):
    """API: таблица клиентов с балансом, tier, рейтингом — с фильтрами и сортировкой."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    tier_filter = request.GET.get('tier', '').strip()
    sort_by = request.GET.get('sort', 'balance')
    sort_dir = request.GET.get('dir', 'desc')
    page = max(int(request.GET.get('page', 1)), 1)
    per_page = min(int(request.GET.get('per_page', 50)), 200)
    offset = (page - 1) * per_page

    allowed_sorts = {
        'balance': 'lb.available_amount',
        'rating': 'ulp.rating',
        'tier': 'ulp.tier',
        'name': 'u.first_name',
        'last_activity': 'ulp.last_activity_at',
    }
    order_col = allowed_sorts.get(sort_by, 'lb.available_amount')
    order_dir = 'ASC' if sort_dir == 'asc' else 'DESC'

    try:
        with connection.cursor() as c:
            where_clauses = ["la.owner_type = 'client'"]
            params = []
            if tier_filter and tier_filter != 'all':
                where_clauses.append("ulp.tier = %s")
                params.append(tier_filter)

            where_sql = ' AND '.join(where_clauses)

            # Count
            c.execute(f"""
                SELECT COUNT(*)
                FROM loyalty_accounts la
                JOIN loyalty_balances lb ON lb.account_id = la.id
                LEFT JOIN user_loyalty_profile ulp ON ulp.user_id = la.owner_id
                LEFT JOIN users u ON u.id = la.owner_id
                WHERE {where_sql}
            """, params)
            total = c.fetchone()[0] or 0

            # Data
            c.execute(f"""
                SELECT u.id, u.first_name, u.last_name, u.inn, u.phone,
                       lb.available_amount, lb.total_earned, lb.total_spent,
                       COALESCE(ulp.tier, 'none') AS tier,
                       COALESCE(ulp.rating, 0) AS rating,
                       ulp.total_orders,
                       ulp.last_activity_at
                FROM loyalty_accounts la
                JOIN loyalty_balances lb ON lb.account_id = la.id
                LEFT JOIN user_loyalty_profile ulp ON ulp.user_id = la.owner_id
                LEFT JOIN users u ON u.id = la.owner_id
                WHERE {where_sql}
                ORDER BY {order_col} {order_dir}
                LIMIT %s OFFSET %s
            """, params + [per_page, offset])
            clients = _dictfetchall(c)

        for c_item in clients:
            if c_item.get('last_activity_at'):
                c_item['last_activity_at'] = c_item['last_activity_at'].isoformat()

        return JsonResponse({
            'ok': True,
            'clients': clients,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page,
        })

    except Exception as e:
        logger.exception("Analytics clients failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


def api_analytics_operations(request):
    """API: агрегация операций по event_type с timeline."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    period = request.GET.get('period', datetime.now().strftime('%Y-%m'))
    group_by = request.GET.get('group', 'day')  # day, week, month

    try:
        p_year, p_month = map(int, period.split('-'))
    except Exception:
        p_year, p_month = datetime.now().year, datetime.now().month

    try:
        with connection.cursor() as c:
            if group_by == 'month':
                date_expr = "DATE_FORMAT(created_at, '%Y-%m')"
            elif group_by == 'week':
                date_expr = "DATE_FORMAT(created_at, '%Y-%u')"
            else:
                date_expr = "DATE(created_at)"

            c.execute(f"""
                SELECT {date_expr} AS period_key,
                       event_type,
                       COUNT(*) AS tx_count,
                       COALESCE(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END), 0) AS credit_sum,
                       COALESCE(SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END), 0) AS debit_sum
                FROM loyalty_transactions
                WHERE YEAR(created_at) = %s AND MONTH(created_at) = %s
                GROUP BY period_key, event_type
                ORDER BY period_key, event_type
            """, [p_year, p_month])
            data = _dictfetchall(c)

        for item in data:
            if hasattr(item.get('period_key'), 'strftime'):
                item['period_key'] = item['period_key'].strftime('%Y-%m-%d')
            else:
                item['period_key'] = str(item['period_key'])

        return JsonResponse({'ok': True, 'data': data})

    except Exception as e:
        logger.exception("Analytics operations failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


def api_analytics_referrals(request):
    """API: воронка рефералов."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    period = request.GET.get('period', datetime.now().strftime('%Y-%m'))
    try:
        p_year, p_month = map(int, period.split('-'))
    except Exception:
        p_year, p_month = datetime.now().year, datetime.now().month

    try:
        with connection.cursor() as c:
            # Всего реферальных бонусов за период
            c.execute("""
                SELECT COUNT(*) AS total_activations,
                       COALESCE(SUM(amount), 0) AS total_bonus
                FROM loyalty_transactions
                WHERE event_type = 'referral_bonus'
                  AND YEAR(created_at) = %s AND MONTH(created_at) = %s
            """, [p_year, p_month])
            summary = _dictfetchone(c)

            # По дням
            c.execute("""
                SELECT DATE(created_at) AS dt, COUNT(*) AS cnt
                FROM loyalty_transactions
                WHERE event_type = 'referral_bonus'
                  AND YEAR(created_at) = %s AND MONTH(created_at) = %s
                GROUP BY DATE(created_at)
                ORDER BY dt
            """, [p_year, p_month])
            timeline = _dictfetchall(c)

        for item in timeline:
            if hasattr(item.get('dt'), 'strftime'):
                item['dt'] = item['dt'].strftime('%Y-%m-%d')

        return JsonResponse({
            'ok': True,
            'summary': summary or {},
            'timeline': timeline,
        })

    except Exception as e:
        logger.exception("Analytics referrals failed")
        return JsonResponse({'error': 'internal_error'}, status=500)


def api_analytics_export(request):
    """Экспорт клиентов с балансами в CSV."""
    user, err = _require_director(request)
    if err:
        return JsonResponse({'error': 'unauthorized'}, status=403)

    try:
        with connection.cursor() as c:
            c.execute("""
                SELECT u.id, u.first_name, u.last_name, u.inn, u.phone,
                       lb.available_amount, lb.reserved_amount,
                       lb.total_earned, lb.total_spent,
                       COALESCE(ulp.tier, 'none') AS tier,
                       COALESCE(ulp.rating, 0) AS rating,
                       ulp.total_orders,
                       ulp.last_activity_at
                FROM loyalty_accounts la
                JOIN loyalty_balances lb ON lb.account_id = la.id
                LEFT JOIN user_loyalty_profile ulp ON ulp.user_id = la.owner_id
                LEFT JOIN users u ON u.id = la.owner_id
                WHERE la.owner_type = 'client'
                ORDER BY lb.available_amount DESC
            """)
            rows = _dictfetchall(c)

        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerow([
            'ID', 'Имя', 'Фамилия', 'ИНН', 'Телефон',
            'Баланс', 'Резерв', 'Всего начислено', 'Всего списано',
            'Уровень', 'Рейтинг', 'Заказов', 'Последняя активность'
        ])
        for r in rows:
            writer.writerow([
                r.get('id', ''), r.get('first_name', ''), r.get('last_name', ''),
                r.get('inn', ''), r.get('phone', ''),
                r.get('available_amount', 0), r.get('reserved_amount', 0),
                r.get('total_earned', 0), r.get('total_spent', 0),
                r.get('tier', ''), r.get('rating', 0),
                r.get('total_orders', 0),
                r.get('last_activity_at', ''),
            ])

        response = HttpResponse(output.getvalue(), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="loyalty_clients_{datetime.now().strftime("%Y%m%d")}.csv"'
        return response

    except Exception as e:
        logger.exception("Analytics export failed")
        return JsonResponse({'error': 'internal_error'}, status=500)
