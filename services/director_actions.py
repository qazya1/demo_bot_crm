"""
Действия директора: жалоба/штраф на партнёра,
ручная корректировка зарплаты, изменение ставки.

Этот модуль используется из Django views (CRM).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from db.partner_finance import (
    update_partner_rating,
    add_salary_adjustment,
    get_salary_period,
)
from services.partner_compensation import (
    set_partner_base_percent,
    calculate_partner_salary,
)
from services.rules_resolver import get_system_number

log = logging.getLogger(__name__)


async def file_complaint(
    specialist_id: int,
    director_id: int,
    comment: str,
    *,
    penalty_points: int | None = None,
    salary_adjustment_rub: float | None = None,
    salary_period_month: str | None = None,
) -> Dict[str, Any]:
    """
    Зарегистрировать жалобу на партнёра.
    Опционально: штраф к рейтингу и/или денежный штраф к зарплате.
    """
    # 1) Штраф рейтинга
    if penalty_points is None:
        penalty_points = int(await get_system_number("LOYALTY_PARTNER_COMPLAINT_RATING_PENALTY", 10) or 10)

    event = await update_partner_rating(
        specialist_id,
        delta=-abs(penalty_points),
        reason="complaint",
        comment=comment,
        initiated_by=director_id,
    )

    # 2) Денежный штраф к зарплате (если указан)
    adjustment = None
    if salary_adjustment_rub and salary_period_month:
        period = await get_salary_period(specialist_id, salary_period_month)
        if period:
            adjustment = await add_salary_adjustment(
                salary_period_id=period["id"],
                adj_type="penalty",
                amount_rub=-abs(salary_adjustment_rub),
                comment=f"Штраф: {comment}",
                created_by=director_id,
            )
            # Пересчитать зарплату
            await calculate_partner_salary(specialist_id, salary_period_month)

    log.info(
        "Complaint filed: specialist=%s by director=%s penalty=%d",
        specialist_id, director_id, penalty_points,
    )

    return {
        "ok": True,
        "rating_event": event,
        "salary_adjustment": adjustment,
    }


async def award_partner_bonus(
    specialist_id: int,
    director_id: int,
    bonus_rub: float,
    salary_period_month: str,
    comment: str,
) -> Dict[str, Any]:
    """Начислить бонус партнёру (ручная корректировка)."""
    period = await get_salary_period(specialist_id, salary_period_month)
    if not period:
        # Создадим период
        await calculate_partner_salary(specialist_id, salary_period_month)
        period = await get_salary_period(specialist_id, salary_period_month)

    if not period:
        return {"ok": False, "error": "cannot_create_period"}

    adjustment = await add_salary_adjustment(
        salary_period_id=period["id"],
        adj_type="bonus",
        amount_rub=abs(bonus_rub),
        comment=comment,
        created_by=director_id,
    )

    # Пересчитать
    await calculate_partner_salary(specialist_id, salary_period_month)

    return {"ok": True, "adjustment": adjustment}


async def change_partner_rate(
    specialist_id: int,
    director_id: int,
    new_percent: float,
    comment: str = "",
) -> Dict[str, Any]:
    """Изменить базовую ставку партнёра."""
    return await set_partner_base_percent(
        specialist_id=specialist_id,
        percent=new_percent,
        director_id=director_id,
        comment=comment,
    )
