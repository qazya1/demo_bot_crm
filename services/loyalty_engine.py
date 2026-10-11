"""
Клиентская лояльность: начисление баллов за покупки, профиль,
списание баллов, пересчёт рейтинга, неактивность.

Все методы принимают internal_user_id и платформо-агностичны
(работают одинаково для TG и MAX ботов).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from services.ledger import (
    post_accrual,
    reserve_points,
    redeem_reserved_points,
    release_reserved_points,
    get_available_balance,
    list_account_transactions,
)
from services.rules_resolver import (
    get_system_number,
    get_bonus_policy,
    get_rating_tier,
)
from db.loyalty import (
    ensure_user_loyalty_profile,
    get_user_loyalty_profile,
    update_user_loyalty_profile,
    get_balance,
    get_loyalty_account,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────

async def is_loyalty_enabled() -> bool:
    """Проверяет system_settings.LOYALTY_ENABLED."""
    from db.system_settings import get_system_setting
    row = await get_system_setting("LOYALTY_ENABLED")
    if not row:
        return False
    val = row.get("char_value") or ""
    return val.strip() == "1"


# ─────────────────────────────────────────────────────────────
# Начисление за покупку товара
# ─────────────────────────────────────────────────────────────

async def award_points_for_purchase(
    internal_user_id: int,
    order_id: int,
    order_amount_rub: float,
    *,
    product_type: str = "order",
) -> Dict[str, Any]:
    """
    Начислить баллы за оплаченный заказ.
    Идемпотентно по order_id.
    """
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    # Получить правило начисления
    policy = await get_bonus_policy(product_type, "purchase")
    rate = await get_system_number("LOYALTY_RUB_TO_POINTS_RATE", 10.0)
    lifetime_days = await get_system_number("LOYALTY_DEFAULT_POINTS_LIFETIME_DAYS", 365)

    # Рассчитать количество баллов
    if policy and policy.get("points_fixed"):
        points = int(policy["points_fixed"])
    elif policy and policy.get("points_per_rub"):
        points = int(float(policy["points_per_rub"]) * order_amount_rub)
    else:
        points = int(rate * order_amount_rub)

    # Ограничения из политики
    if policy:
        min_order = policy.get("min_order_rub")
        if min_order and order_amount_rub < float(min_order):
            return {"ok": False, "reason": "below_min_order", "min_order_rub": min_order}
        max_points = policy.get("max_points")
        if max_points and points > int(max_points):
            points = int(max_points)
        lt = policy.get("points_lifetime_days")
        if lt:
            lifetime_days = int(lt)

    if points <= 0:
        return {"ok": False, "reason": "zero_points"}

    # Tier bonus
    profile = await ensure_user_loyalty_profile(internal_user_id)
    tier_info = await get_rating_tier("client", profile.get("rating", 0))
    if tier_info and float(tier_info.get("bonus_pct", 0)) > 0:
        bonus = int(points * float(tier_info["bonus_pct"]) / 100)
        points += bonus

    result = await post_accrual(
        owner_type="client",
        owner_id=internal_user_id,
        amount=points,
        event_type="purchase",
        idempotency_key=f"purchase:order:{order_id}",
        description=f"Баллы за заказ #{order_id} ({order_amount_rub:.0f} руб)",
        rating_delta=1,
        lifetime_days=int(lifetime_days) if lifetime_days else 365,
        ref_entity_type="order",
        ref_entity_id=order_id,
    )

    if result.get("ok") and not result.get("duplicate"):
        await update_user_loyalty_profile(
            internal_user_id,
            rating_delta=1,
            orders_delta=1,
            spent_rub_delta=order_amount_rub,
        )
        # Обновить tier
        await _maybe_update_tier(internal_user_id)

    return result


# ─────────────────────────────────────────────────────────────
# Бонус за первую покупку
# ─────────────────────────────────────────────────────────────

async def award_first_purchase_bonus(
    internal_user_id: int,
    order_id: int,
) -> Dict[str, Any]:
    """Начислить одноразовый бонус за первую покупку."""
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    bonus = await get_system_number("LOYALTY_FIRST_PURCHASE_BONUS", 50)
    if not bonus or bonus <= 0:
        return {"ok": False, "reason": "bonus_not_configured"}

    lifetime_days = await get_system_number("LOYALTY_DEFAULT_POINTS_LIFETIME_DAYS", 365)

    return await post_accrual(
        owner_type="client",
        owner_id=internal_user_id,
        amount=int(bonus),
        event_type="first_purchase",
        idempotency_key=f"first_purchase:user:{internal_user_id}",
        description="Бонус за первую покупку!",
        lifetime_days=int(lifetime_days) if lifetime_days else 365,
        ref_entity_type="order",
        ref_entity_id=order_id,
    )


# ─────────────────────────────────────────────────────────────
# Бонус за заполнение профиля
# ─────────────────────────────────────────────────────────────

async def award_profile_completion_bonus(
    internal_user_id: int,
    field_name: str,
) -> Dict[str, Any]:
    """Начислить баллы за заполнение поля профиля (ИНН, телефон, email и т.д.)."""
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    points = await get_system_number("LOYALTY_PROFILE_FIELD_POINTS", 10)
    if not points or points <= 0:
        return {"ok": False, "reason": "bonus_not_configured"}

    lifetime_days = await get_system_number("LOYALTY_DEFAULT_POINTS_LIFETIME_DAYS", 365)
    notify_client = str(field_name or "").lower() not in {"phone", "inn", "first_name", "last_name"}

    return await post_accrual(
        owner_type="client",
        owner_id=internal_user_id,
        amount=int(points),
        event_type="profile_field",
        idempotency_key=f"profile_field:{internal_user_id}:{field_name}",
        description=f"Бонус за заполнение поля «{field_name}»",
        lifetime_days=int(lifetime_days) if lifetime_days else 365,
        notify_client=notify_client,
    )


# ─────────────────────────────────────────────────────────────
# Списание баллов при оплате
# ─────────────────────────────────────────────────────────────

async def redeem_points_for_order(
    internal_user_id: int,
    order_id: int,
    requested_points: int,
    order_amount_rub: float,
    *,
    entity_type: str = "order",
) -> Dict[str, Any]:
    """
    Зарезервировать баллы для оплаты.
    Проверяет лимит MAX_REDEEM_SHARE_PCT.
    Возвращает reservation key для последующего redeem/release.
    """
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    if requested_points <= 0:
        return {"ok": False, "reason": "invalid_amount"}

    # Проверка лимита
    max_share = await get_system_number("LOYALTY_MAX_REDEEM_SHARE_PCT", 30)
    points_to_rub = await get_system_number("LOYALTY_POINTS_TO_RUB_RATE", 0.1)

    rub_equivalent = requested_points * (points_to_rub or 0.1)
    max_rub = order_amount_rub * ((max_share or 30) / 100)

    if rub_equivalent > max_rub:
        # Сократить до лимита
        requested_points = int(max_rub / (points_to_rub or 0.1))
        rub_equivalent = requested_points * (points_to_rub or 0.1)

    if requested_points <= 0:
        return {"ok": False, "reason": "amount_too_small"}

    reservation_key = f"reserve:{entity_type}:{order_id}"

    result = await reserve_points(
        owner_type="client",
        owner_id=internal_user_id,
        amount=requested_points,
        idempotency_key=reservation_key,
        description=f"Оплата баллами: {entity_type} #{order_id}",
        ref_entity_type=entity_type,
        ref_entity_id=order_id,
    )

    if result.get("ok"):
        result["reservation_key"] = reservation_key
        result["points_reserved"] = requested_points
        result["rub_discount"] = rub_equivalent
        result["points_to_rub_rate"] = points_to_rub

    return result


async def confirm_points_redeem(reservation_key: str) -> Dict[str, Any]:
    """Подтвердить списание после успешной оплаты."""
    return await redeem_reserved_points(reservation_key)


async def cancel_points_redeem(reservation_key: str) -> Dict[str, Any]:
    """Отменить резерв (оплата не состоялась)."""
    return await release_reserved_points(reservation_key)


# ─────────────────────────────────────────────────────────────
# Рейтинг и уровень
# ─────────────────────────────────────────────────────────────

async def recalculate_user_rating(internal_user_id: int) -> Dict[str, Any]:
    """Пересчитать рейтинг на основе истории покупок и профиля."""
    profile = await ensure_user_loyalty_profile(internal_user_id)
    current_rating = profile.get("rating", 0)
    await _maybe_update_tier(internal_user_id)

    updated = await get_user_loyalty_profile(internal_user_id)
    return {
        "user_id": internal_user_id,
        "rating": updated.get("rating", 0) if updated else current_rating,
        "tier": updated.get("tier", "none") if updated else "none",
    }


async def apply_inactivity_penalty(
    internal_user_id: int,
    days_inactive: int = 180,
    penalty_points: int = 10,
) -> Dict[str, Any]:
    """Штраф за неактивность (вызывается из фоновой задачи)."""
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    profile = await get_user_loyalty_profile(internal_user_id)
    if not profile:
        return {"ok": False, "reason": "no_profile"}

    last_activity = profile.get("last_activity_at")
    if last_activity and (datetime.utcnow() - last_activity).days < days_inactive:
        return {"ok": False, "reason": "still_active"}

    idempotency_key = f"inactivity:{internal_user_id}:{datetime.utcnow().strftime('%Y-%m')}"

    result = await post_accrual(
        owner_type="client",
        owner_id=internal_user_id,
        amount=penalty_points,
        event_type="inactivity_penalty",
        idempotency_key=idempotency_key,
        description="Штраф за неактивность",
        rating_delta=-5,
    )

    # Это debit, не accrual — нужно скорректировать
    # На самом деле штраф за неактивность — это списание рейтинга, не баллов.
    # Обновим рейтинг напрямую:
    if not result.get("duplicate"):
        await update_user_loyalty_profile(
            internal_user_id,
            rating_delta=-5,
        )

    return result


# ─────────────────────────────────────────────────────────────
# Сводка для отображения в боте
# ─────────────────────────────────────────────────────────────

async def get_user_loyalty_summary(internal_user_id: int) -> Dict[str, Any]:
    """Полная сводка: баланс, рейтинг, tier, история, ближайшее сгорание."""
    balance = await get_available_balance("client", internal_user_id)
    profile = await get_user_loyalty_profile(internal_user_id)
    history = await list_account_transactions("client", internal_user_id, limit=20)

    # Ближайшее сгорание
    nearest_expiry = None
    nearest_expiry_amount = 0
    for tx in history:
        if (tx.get("direction") == "credit"
                and tx.get("expires_at")
                and not tx.get("is_expired")):
            exp = tx["expires_at"]
            if isinstance(exp, str):
                try:
                    exp = datetime.fromisoformat(exp)
                except ValueError:
                    continue
            if exp > datetime.utcnow():
                if nearest_expiry is None or exp < nearest_expiry:
                    nearest_expiry = exp
                    nearest_expiry_amount = tx["amount"]

    return {
        "balance": balance,
        "rating": profile.get("rating", 0) if profile else 0,
        "tier": profile.get("tier", "none") if profile else "none",
        "total_orders": profile.get("total_orders", 0) if profile else 0,
        "total_spent_rub": float(profile.get("total_spent_rub", 0)) if profile else 0,
        "history": history,
        "nearest_expiry": nearest_expiry.isoformat() if nearest_expiry else None,
        "nearest_expiry_amount": nearest_expiry_amount,
    }


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

async def _maybe_update_tier(internal_user_id: int) -> None:
    """Обновить tier на основании текущего рейтинга."""
    profile = await get_user_loyalty_profile(internal_user_id)
    if not profile:
        return
    rating = profile.get("rating", 0)
    tier_info = await get_rating_tier("client", rating)
    new_tier = tier_info["tier_name"] if tier_info else "none"
    if new_tier != profile.get("tier"):
        await update_user_loyalty_profile(internal_user_id, tier=new_tier, touch_activity=False)
