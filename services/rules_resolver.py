"""
Единая точка получения правил и настроек.

Абстрагирует доступ к system_settings, bonus_product_policies,
rating_tiers и partner_financial_profile.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from db.connection import get_conn, _cur
from db.system_settings import get_system_setting_number


# ─────────────────────────────────────────────────────────────
# System settings
# ─────────────────────────────────────────────────────────────

async def get_system_number(name: str, default: float | int | None = None) -> Optional[float]:
    """Обёртка над db.get_system_setting_number()."""
    return await get_system_setting_number(name, default)


# ─────────────────────────────────────────────────────────────
# Bonus product policies
# ─────────────────────────────────────────────────────────────

async def get_bonus_policy(
    product_type: str,
    event_type: str,
    at_datetime: datetime | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Найти подходящую политику начисления.
    Выбирает активную с наивысшим приоритетом, попадающую в valid_from/valid_until.
    """
    now = at_datetime or datetime.utcnow()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT *
                FROM bonus_product_policies
                WHERE product_type = %s
                  AND event_type = %s
                  AND is_active = 1
                  AND (valid_from IS NULL OR valid_from <= %s)
                  AND (valid_until IS NULL OR valid_until >= %s)
                ORDER BY priority DESC
                LIMIT 1
                """,
                (product_type, event_type, now, now),
            )
            return await c.fetchone()


# ─────────────────────────────────────────────────────────────
# Rating tiers
# ─────────────────────────────────────────────────────────────

async def get_rating_tier(
    target_type: str,
    rating: int,
) -> Optional[Dict[str, Any]]:
    """
    Определить уровень (tier) по рейтингу.
    Возвращает tier с наибольшим min_rating, не превышающим текущий rating.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT *
                FROM rating_tiers
                WHERE target_type = %s
                  AND min_rating <= %s
                  AND is_active = 1
                ORDER BY min_rating DESC
                LIMIT 1
                """,
                (target_type, rating),
            )
            return await c.fetchone()


# ─────────────────────────────────────────────────────────────
# Partner base percent
# ─────────────────────────────────────────────────────────────

async def get_partner_base_percent(specialist_id: int) -> float:
    """
    Эффективная ставка партнёра (override если есть, иначе base).
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT base_revenue_pct, override_revenue_pct
                FROM partner_financial_profile
                WHERE specialist_id = %s
                LIMIT 1
                """,
                (specialist_id,),
            )
            row = await c.fetchone()
            if not row:
                default = await get_system_number("LOYALTY_PARTNER_DEFAULT_BASE_REVENUE_PCT", 50)
                return float(default) if default else 50.0

            override = row.get("override_revenue_pct")
            if override is not None:
                return float(override)
            return float(row.get("base_revenue_pct", 50))
