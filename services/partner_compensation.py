"""
Партнёрская компенсация: рейтинг, ставка, расчёт зарплаты.

Все расчёты основаны на данных support_chat_payments за месяц.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from db.connection import get_conn, _cur, _maybe_await
from db.partner_finance import (
    ensure_partner_financial_profile,
    get_partner_financial_profile,
    set_partner_base_percent as db_set_partner_base_percent,
    set_partner_override_percent,
    update_partner_rating,
    list_partner_rating_events,
    get_salary_period,
    upsert_salary_period,
    set_salary_period_status,
    list_salary_periods,
    add_salary_adjustment,
    list_salary_adjustments,
)
from services.rules_resolver import get_system_number, get_rating_tier

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Рейтинг партнёра
# ─────────────────────────────────────────────────────────────

async def recalculate_partner_rating(
    specialist_id: int,
    period_month: str,
) -> Dict[str, Any]:
    """
    Пересчитать рейтинг партнёра за месяц.
    Учитывает: кол-во консультаций, средняя оценка (если есть), жалобы.
    """
    profile = await ensure_partner_financial_profile(
        specialist_id,
        base_revenue_pct=float(await get_system_number("LOYALTY_PARTNER_DEFAULT_BASE_REVENUE_PCT", 50) or 50),
        initial_rating=int(await get_system_number("LOYALTY_PARTNER_INITIAL_RATING", 100) or 100),
    )

    # Подсчитать метрики за месяц
    stats = await _get_partner_month_stats(specialist_id, period_month)
    chats_count = stats.get("chats_count", 0)
    paid_count = stats.get("paid_count", 0)

    # Рейтинговая дельта: +1 за каждую оплаченную консультацию
    delta = paid_count

    if delta != 0:
        event = await update_partner_rating(
            specialist_id,
            delta=delta,
            reason="monthly_review",
            comment=f"Месяц {period_month}: {paid_count} оплат, {chats_count} чатов",
        )
    else:
        event = None

    # Обновить tier
    updated_profile = await get_partner_financial_profile(specialist_id)
    new_rating = updated_profile["rating"] if updated_profile else profile["rating"]
    tier_info = await get_rating_tier("partner", new_rating)
    new_tier = tier_info["tier_name"] if tier_info else "standard"

    if updated_profile and updated_profile.get("rating_tier") != new_tier:
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    "UPDATE partner_financial_profile SET rating_tier = %s WHERE specialist_id = %s",
                    (new_tier, specialist_id),
                )
                await _maybe_await(conn.commit())

    return {
        "specialist_id": specialist_id,
        "rating": new_rating,
        "tier": new_tier,
        "delta": delta,
        "event": event,
    }


# ─────────────────────────────────────────────────────────────
# Выручка партнёра
# ─────────────────────────────────────────────────────────────

async def calculate_partner_revenue(
    specialist_id: int,
    period_month: str,
) -> Decimal:
    """Суммарная выручка партнёра за месяц (из support_chat_payments)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN salary_base_amount_rub IS NOT NULL
                         THEN salary_base_amount_rub
                         ELSE amount_rub
                    END
                ), 0) AS total_revenue
                FROM support_chat_payments
                WHERE specialist_id = %s
                  AND status = 'CONFIRMED'
                  AND DATE_FORMAT(created_at, '%%Y-%%m') = %s
                """,
                (specialist_id, period_month),
            )
            row = await c.fetchone()
            return Decimal(str(row["total_revenue"])) if row else Decimal("0")


# ─────────────────────────────────────────────────────────────
# Расчёт зарплаты
# ─────────────────────────────────────────────────────────────

async def calculate_partner_salary(
    specialist_id: int,
    period_month: str,
) -> Dict[str, Any]:
    """
    Рассчитать зарплату партнёра за месяц.
    Создаёт/обновляет partner_salary_periods в статусе 'draft'.
    """
    profile = await ensure_partner_financial_profile(
        specialist_id,
        base_revenue_pct=float(await get_system_number("LOYALTY_PARTNER_DEFAULT_BASE_REVENUE_PCT", 50) or 50),
        initial_rating=int(await get_system_number("LOYALTY_PARTNER_INITIAL_RATING", 100) or 100),
    )

    # Выручка
    revenue = await calculate_partner_revenue(specialist_id, period_month)
    referred_revenue = await _get_partner_referred_revenue(specialist_id, period_month)
    referral_uplift_pct = float(await get_system_number("LOYALTY_PARTNER_REFERRAL_UPLIFT_PCT", 0) or 0)

    # Эффективная ставка
    override = profile.get("override_revenue_pct")
    base_pct = float(override if override is not None else profile.get("base_revenue_pct", 50))

    # Tier bonus
    rating = profile.get("rating", 0)
    tier_info = await get_rating_tier("partner", rating)
    tier_bonus_pct = float(tier_info.get("bonus_pct", 0)) if tier_info else 0
    effective_pct = base_pct + tier_bonus_pct

    # Базовая зарплата + надбавка на выручку приглашённых этим партнёром клиентов
    salary_base_amount = revenue * Decimal(str(effective_pct)) / Decimal("100")
    referral_uplift_amount = referred_revenue * Decimal(str(referral_uplift_pct)) / Decimal("100")
    base_salary = salary_base_amount + referral_uplift_amount

    # Существующие корректировки
    existing = await get_salary_period(specialist_id, period_month)
    adjustments_rub = Decimal("0")
    if existing:
        adjustments_rub = Decimal(str(existing.get("adjustments_rub", 0)))

    final_salary = base_salary + adjustments_rub

    period_id = await upsert_salary_period(
        specialist_id,
        period_month,
        gross_revenue_rub=float(revenue),
        effective_pct=float(effective_pct),
        base_salary_rub=float(base_salary),
        adjustments_rub=float(adjustments_rub),
        final_salary_rub=float(final_salary),
    )

    return {
        "period_id": period_id,
        "specialist_id": specialist_id,
        "period_month": period_month,
        "gross_revenue_rub": float(revenue),
        "effective_pct": effective_pct,
        "base_salary_rub": float(base_salary),
        "referral_uplift_rub": float(referral_uplift_amount),
        "adjustments_rub": float(adjustments_rub),
        "final_salary_rub": float(final_salary),
    }


async def rebuild_salary_period(
    specialist_id: int,
    period_month: str,
) -> Dict[str, Any]:
    """Пересчитать зарплатный период с нуля (включая корректировки)."""
    # Пересчитать рейтинг
    await recalculate_partner_rating(specialist_id, period_month)
    # Пересчитать зарплату
    return await calculate_partner_salary(specialist_id, period_month)


# ─────────────────────────────────────────────────────────────
# Ручные корректировки директора
# ─────────────────────────────────────────────────────────────

async def apply_manual_salary_adjustment(
    salary_period_id: int,
    adj_type: str,
    amount: float,
    comment: str,
    director_id: int,
) -> Dict[str, Any]:
    """Добавить ручную корректировку к зарплате партнёра."""
    adjustment = await add_salary_adjustment(
        salary_period_id=salary_period_id,
        adj_type=adj_type,
        amount_rub=amount,
        comment=comment,
        created_by=director_id,
    )
    return {"ok": True, "adjustment": adjustment}


async def set_partner_base_percent(
    specialist_id: int,
    percent: float,
    director_id: int,
    comment: str = "",
) -> Dict[str, Any]:
    """Установить базовую ставку партнёра (с записью в рейтинговые события)."""
    profile = await get_partner_financial_profile(specialist_id)
    old_pct = float(profile.get("base_revenue_pct", 50)) if profile else 50

    await db_set_partner_base_percent(specialist_id, percent)

    if comment:
        await update_partner_rating(
            specialist_id,
            delta=0,
            reason="manual",
            comment=f"Ставка изменена: {old_pct}% → {percent}%. {comment}",
            initiated_by=director_id,
        )

    return {"ok": True, "old_pct": old_pct, "new_pct": percent}


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

async def _get_partner_month_stats(
    specialist_id: int,
    period_month: str,
) -> Dict[str, int]:
    """Статистика партнёра за месяц: кол-во чатов, оплат."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Количество чатов
            await c.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM support_chats
                WHERE specialist_id = %s
                  AND DATE_FORMAT(created_at, '%%Y-%%m') = %s
                """,
                (specialist_id, period_month),
            )
            chats_row = await c.fetchone()

            # Количество оплаченных
            await c.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM support_chat_payments
                WHERE specialist_id = %s
                  AND status = 'CONFIRMED'
                  AND DATE_FORMAT(created_at, '%%Y-%%m') = %s
                """,
                (specialist_id, period_month),
            )
            paid_row = await c.fetchone()

            return {
                "chats_count": int(chats_row["cnt"]) if chats_row else 0,
                "paid_count": int(paid_row["cnt"]) if paid_row else 0,
            }


async def _get_partner_referred_revenue(
    specialist_id: int,
    period_month: str,
) -> Decimal:
    """Выручка за период только по приглашённым этим специалистом клиентам."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN scp.salary_base_amount_rub IS NOT NULL
                         THEN scp.salary_base_amount_rub
                         ELSE scp.amount_rub
                    END
                ), 0) AS total_revenue
                FROM support_chat_payments scp
                JOIN support_chats sch ON sch.id = scp.chat_id
                JOIN referral_activations ra ON ra.user_id = CAST(sch.client_user_id AS CHAR)
                JOIN referral_codes rc ON rc.id = ra.referral_code_id
                WHERE scp.specialist_id = %s
                  AND scp.status = 'CONFIRMED'
                  AND DATE_FORMAT(scp.created_at, '%%Y-%%m') = %s
                  AND rc.owner_note = CONCAT('specialist:', CAST(%s AS CHAR))
                """,
                (specialist_id, period_month, specialist_id),
            )
            row = await c.fetchone()
            return Decimal(str(row["total_revenue"])) if row else Decimal("0")
