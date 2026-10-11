"""
Хелперы для ботов: списание баллов при оплате.

Используется из bot_catalog / max_catalog (товары)
и bot_client_chat / max_client_chat (услуги).
"""
from __future__ import annotations

from typing import Any, Dict

from services.loyalty_engine import (
    is_loyalty_enabled,
    get_user_loyalty_summary,
    redeem_points_for_order,
    confirm_points_redeem,
    cancel_points_redeem,
)
from services.rules_resolver import get_system_number


async def get_bonus_checkout_info(
    internal_user_id: int,
    order_amount_rub: float,
) -> Dict[str, Any]:
    """
    Информация для UI: сколько баллов доступно, максимум списания, курс.
    Возвращает dict для формирования клавиатуры/текста.
    """
    if not await is_loyalty_enabled():
        return {"available": False}

    summary = await get_user_loyalty_summary(internal_user_id)
    balance = summary.get("balance", 0)

    if balance <= 0:
        return {"available": False, "balance": 0}

    points_to_rub = await get_system_number("LOYALTY_POINTS_TO_RUB_RATE", 0.1)
    max_share_pct = await get_system_number("LOYALTY_MAX_REDEEM_SHARE_PCT", 30)

    max_rub = order_amount_rub * ((max_share_pct or 30) / 100)
    max_points = int(max_rub / (points_to_rub or 0.1))

    usable_points = min(balance, max_points)
    usable_rub = usable_points * (points_to_rub or 0.1)

    return {
        "available": True,
        "balance": balance,
        "usable_points": usable_points,
        "usable_rub_discount": round(usable_rub, 2),
        "points_to_rub_rate": points_to_rub or 0.1,
        "max_share_pct": max_share_pct or 30,
    }


async def apply_bonus_discount(
    internal_user_id: int,
    order_id: int,
    requested_points: int,
    order_amount_rub: float,
    *,
    entity_type: str = "order",
) -> Dict[str, Any]:
    """
    Зарезервировать баллы для оплаты.
    Возвращает reservation_key для последующего подтверждения/отмены.
    """
    return await redeem_points_for_order(
        internal_user_id=internal_user_id,
        order_id=order_id,
        requested_points=requested_points,
        order_amount_rub=order_amount_rub,
        entity_type=entity_type,
    )


async def finalize_bonus_discount(reservation_key: str) -> Dict[str, Any]:
    """Подтвердить списание (оплата прошла)."""
    return await confirm_points_redeem(reservation_key)


async def cancel_bonus_discount(reservation_key: str) -> Dict[str, Any]:
    """Отменить резерв (оплата не состоялась)."""
    return await cancel_points_redeem(reservation_key)
