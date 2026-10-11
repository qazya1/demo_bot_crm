"""
CRUD-операции для таблиц партнёрской компенсации:
- partner_financial_profile
- partner_rating_events
- partner_salary_periods
- partner_salary_adjustments
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from db.connection import get_conn, _cur, _maybe_await, _begin_tx


# ─────────────────────────────────────────────────────────────
# partner_financial_profile
# ─────────────────────────────────────────────────────────────

async def ensure_partner_financial_profile(
    specialist_id: int,
    *,
    base_revenue_pct: float = 50.0,
    initial_rating: int = 100,
) -> Dict[str, Any]:
    """Создаёт финансовый профиль партнёра, если ещё нет. Возвращает текущий."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT IGNORE INTO partner_financial_profile
                    (specialist_id, base_revenue_pct, rating)
                VALUES (%s, %s, %s)
                """,
                (specialist_id, base_revenue_pct, initial_rating),
            )
            await _maybe_await(conn.commit())

            await c.execute(
                "SELECT * FROM partner_financial_profile WHERE specialist_id = %s LIMIT 1",
                (specialist_id,),
            )
            return await c.fetchone()


async def get_partner_financial_profile(specialist_id: int) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT * FROM partner_financial_profile WHERE specialist_id = %s LIMIT 1",
                (specialist_id,),
            )
            return await c.fetchone()


async def set_partner_base_percent(
    specialist_id: int,
    percent: float,
) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE partner_financial_profile
                SET base_revenue_pct = %s
                WHERE specialist_id = %s
                """,
                (percent, specialist_id),
            )
            await _maybe_await(conn.commit())


async def set_partner_override_percent(
    specialist_id: int,
    percent: float | None,
) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE partner_financial_profile
                SET override_revenue_pct = %s
                WHERE specialist_id = %s
                """,
                (percent, specialist_id),
            )
            await _maybe_await(conn.commit())


async def update_partner_rating(
    specialist_id: int,
    delta: int,
    reason: str,
    *,
    comment: str | None = None,
    initiated_by: int | None = None,
    ref_entity_type: str | None = None,
    ref_entity_id: int | None = None,
) -> Dict[str, Any]:
    """Изменяет рейтинг партнёра и записывает событие. Возвращает событие."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            await c.execute(
                "SELECT rating FROM partner_financial_profile WHERE specialist_id = %s FOR UPDATE",
                (specialist_id,),
            )
            row = await c.fetchone()
            rating_before = row["rating"] if row else 0
            rating_after = max(0, rating_before + delta)

            await c.execute(
                """
                UPDATE partner_financial_profile
                SET rating = %s
                WHERE specialist_id = %s
                """,
                (rating_after, specialist_id),
            )

            await c.execute(
                """
                INSERT INTO partner_rating_events
                    (specialist_id, delta, reason, comment, initiated_by,
                     rating_before, rating_after, ref_entity_type, ref_entity_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    specialist_id, delta, reason, comment, initiated_by,
                    rating_before, rating_after, ref_entity_type, ref_entity_id,
                ),
            )
            event_id = c.lastrowid

            await _maybe_await(conn.commit())

            await c.execute(
                "SELECT * FROM partner_rating_events WHERE id = %s", (event_id,)
            )
            return await c.fetchone()


async def list_partner_rating_events(
    specialist_id: int,
    limit: int = 20,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM partner_rating_events
                WHERE specialist_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (specialist_id, limit, offset),
            )
            return await c.fetchall() or []


# ─────────────────────────────────────────────────────────────
# partner_salary_periods
# ─────────────────────────────────────────────────────────────

async def get_salary_period(
    specialist_id: int,
    period_month: str,
) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM partner_salary_periods
                WHERE specialist_id = %s AND period_month = %s
                LIMIT 1
                """,
                (specialist_id, period_month),
            )
            return await c.fetchone()


async def upsert_salary_period(
    specialist_id: int,
    period_month: str,
    *,
    gross_revenue_rub: float = 0,
    effective_pct: float = 0,
    base_salary_rub: float = 0,
    adjustments_rub: float = 0,
    final_salary_rub: float = 0,
) -> int:
    """Создаёт или обновляет зарплатный период (draft). Возвращает id."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO partner_salary_periods
                    (specialist_id, period_month, gross_revenue_rub, effective_pct,
                     base_salary_rub, adjustments_rub, final_salary_rub)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    gross_revenue_rub = VALUES(gross_revenue_rub),
                    effective_pct = VALUES(effective_pct),
                    base_salary_rub = VALUES(base_salary_rub),
                    adjustments_rub = VALUES(adjustments_rub),
                    final_salary_rub = VALUES(final_salary_rub)
                """,
                (
                    specialist_id, period_month, gross_revenue_rub, effective_pct,
                    base_salary_rub, adjustments_rub, final_salary_rub,
                ),
            )
            period_id = c.lastrowid
            await _maybe_await(conn.commit())

            if not period_id:
                row = await get_salary_period(specialist_id, period_month)
                return row["id"]
            return period_id


async def set_salary_period_status(
    period_id: int,
    status: str,
    *,
    approved_by: int | None = None,
) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if status == "approved":
                await c.execute(
                    """
                    UPDATE partner_salary_periods
                    SET status = 'approved', approved_by = %s, approved_at = NOW()
                    WHERE id = %s AND status = 'draft'
                    """,
                    (approved_by, period_id),
                )
            elif status == "paid":
                await c.execute(
                    """
                    UPDATE partner_salary_periods
                    SET status = 'paid', paid_at = NOW()
                    WHERE id = %s AND status = 'approved'
                    """,
                    (period_id,),
                )
            await _maybe_await(conn.commit())


async def list_salary_periods(
    specialist_id: int | None = None,
    period_month: str | None = None,
    status: str | None = None,
) -> List[Dict[str, Any]]:
    conditions = []
    params = []
    if specialist_id is not None:
        conditions.append("specialist_id = %s")
        params.append(specialist_id)
    if period_month is not None:
        conditions.append("period_month = %s")
        params.append(period_month)
    if status is not None:
        conditions.append("status = %s")
        params.append(status)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"""
                SELECT * FROM partner_salary_periods
                {where}
                ORDER BY period_month DESC, specialist_id ASC
                """,
                tuple(params),
            )
            return await c.fetchall() or []


# ─────────────────────────────────────────────────────────────
# partner_salary_adjustments
# ─────────────────────────────────────────────────────────────

async def add_salary_adjustment(
    salary_period_id: int,
    adj_type: str,
    amount_rub: float,
    comment: str,
    created_by: int,
) -> Dict[str, Any]:
    """Добавляет ручную корректировку и обновляет итог периода."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            await c.execute(
                """
                INSERT INTO partner_salary_adjustments
                    (salary_period_id, type, amount_rub, comment, created_by)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (salary_period_id, adj_type, amount_rub, comment, created_by),
            )
            adj_id = c.lastrowid

            # Пересчитать сумму корректировок и итог
            await c.execute(
                """
                UPDATE partner_salary_periods psp
                SET psp.adjustments_rub = (
                        SELECT COALESCE(SUM(a.amount_rub), 0)
                        FROM partner_salary_adjustments a
                        WHERE a.salary_period_id = psp.id
                    ),
                    psp.final_salary_rub = psp.base_salary_rub + (
                        SELECT COALESCE(SUM(a.amount_rub), 0)
                        FROM partner_salary_adjustments a
                        WHERE a.salary_period_id = psp.id
                    )
                WHERE psp.id = %s
                """,
                (salary_period_id,),
            )

            await _maybe_await(conn.commit())

            await c.execute(
                "SELECT * FROM partner_salary_adjustments WHERE id = %s", (adj_id,)
            )
            return await c.fetchone()


async def list_salary_adjustments(salary_period_id: int) -> List[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM partner_salary_adjustments
                WHERE salary_period_id = %s
                ORDER BY created_at ASC
                """,
                (salary_period_id,),
            )
            return await c.fetchall() or []
