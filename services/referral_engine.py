"""
Реферальные награды + QR-коллекция.

Обрабатывает:
- Начисление бонуса рефереру при активации приглашённого
- Начисление бонуса при верификации приглашённого
- Регистрация QR-сканов + награда при достижении порога
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from services.ledger import post_accrual, get_available_balance
from services.rules_resolver import get_system_number
from db.connection import get_conn, _cur, _maybe_await

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Реферальная активация
# ─────────────────────────────────────────────────────────────

async def award_referral_activation(
    referrer_user_id: int,
    invited_user_id: int,
) -> Dict[str, Any]:
    """
    Начислить бонус рефереру за привлечение нового пользователя.
    Идемпотентно по паре referrer+invited.
    """
    from services.loyalty_engine import is_loyalty_enabled
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    points = await get_system_number("LOYALTY_REFERRAL_ACTIVATION_POINTS", 50)
    if not points or points <= 0:
        return {"ok": False, "reason": "no_points_configured"}

    rating_delta = await get_system_number("LOYALTY_REFERRAL_ACTIVATION_RATING_DELTA", 2)
    lifetime_days = await get_system_number("LOYALTY_DEFAULT_POINTS_LIFETIME_DAYS", 365)

    return await post_accrual(
        owner_type="client",
        owner_id=referrer_user_id,
        amount=int(points),
        event_type="referral_activation",
        idempotency_key=f"referral_activation:{referrer_user_id}:{invited_user_id}",
        description=f"Бонус за приглашение пользователя #{invited_user_id}",
        rating_delta=int(rating_delta) if rating_delta is not None else 2,
        lifetime_days=int(lifetime_days) if lifetime_days else 365,
        ref_entity_type="referral",
        ref_entity_id=invited_user_id,
    )


# ─────────────────────────────────────────────────────────────
# Верификация приглашённого
# ─────────────────────────────────────────────────────────────

async def award_referral_verification(
    invited_user_id: int,
) -> Dict[str, Any]:
    """
    Начислить бонус рефереру, когда приглашённый прошёл верификацию.
    Находит реферера через referral_activations.
    """
    from services.loyalty_engine import is_loyalty_enabled
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    # Найти реферера
    referrer_id = await _find_referrer(invited_user_id)
    if not referrer_id:
        return {"ok": False, "reason": "no_referrer"}

    points = await get_system_number("LOYALTY_REFERRAL_VERIFICATION_POINTS", 100)
    if not points or points <= 0:
        return {"ok": False, "reason": "no_points_configured"}

    rating_delta = await get_system_number("LOYALTY_REFERRAL_VERIFICATION_RATING_DELTA", 3)
    lifetime_days = await get_system_number("LOYALTY_DEFAULT_POINTS_LIFETIME_DAYS", 365)

    l1_result = await post_accrual(
        owner_type="client",
        owner_id=int(referrer_id),
        amount=int(points),
        event_type="referral_verification",
        idempotency_key=f"referral_verification:{invited_user_id}",
        description=f"Бонус за верификацию приглашённого #{invited_user_id}",
        rating_delta=int(rating_delta) if rating_delta is not None else 3,
        lifetime_days=int(lifetime_days) if lifetime_days else 365,
        ref_entity_type="referral",
        ref_entity_id=invited_user_id,
    )

    # L2: бонус тому, кто пригласил реферера (только за верификацию)
    l2_referrer_id = await _find_referrer(int(referrer_id))
    if not l2_referrer_id or int(l2_referrer_id) == int(referrer_id):
        return l1_result

    l2_points = await get_system_number("LOYALTY_REFERRAL_L2_VERIFICATION_POINTS", 0)
    if not l2_points or l2_points <= 0:
        return l1_result

    l2_rating_delta = await get_system_number("LOYALTY_REFERRAL_L2_VERIFICATION_RATING_DELTA", 0)

    await post_accrual(
        owner_type="client",
        owner_id=int(l2_referrer_id),
        amount=int(l2_points),
        event_type="referral_verification_l2",
        idempotency_key=f"referral_verification_l2:{l2_referrer_id}:{invited_user_id}",
        description=f"L2-бонус за верификацию пользователя #{invited_user_id}",
        rating_delta=int(l2_rating_delta) if l2_rating_delta is not None else 0,
        lifetime_days=int(lifetime_days) if lifetime_days else 365,
        ref_entity_type="referral",
        ref_entity_id=invited_user_id,
    )

    return l1_result


# ─────────────────────────────────────────────────────────────
# QR-коллекция
# ─────────────────────────────────────────────────────────────

async def register_qr_scan(
    owner_user_id: int,
    invited_user_id: int,
    referral_code_id: int,
    scan_source: str = "qr",
) -> Dict[str, Any]:
    """
    Зарегистрировать QR-скан (расширяет referral_activations).
    Возвращает текущий прогресс.
    """
    from services.loyalty_engine import is_loyalty_enabled
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Обновить scan_source если ещё не заполнен
            await c.execute(
                """
                UPDATE referral_activations
                SET scan_source = COALESCE(scan_source, %s)
                WHERE user_id = %s AND referral_code_id = %s
                """,
                (scan_source, str(invited_user_id), referral_code_id),
            )
            await _maybe_await(conn.commit())

    progress = await get_referral_progress(owner_user_id)
    return {"ok": True, "progress": progress}


async def process_qr_collection_reward(
    owner_user_id: int,
) -> Dict[str, Any]:
    """
    Проверить порог QR-коллекции и начислить награду, если достигнут.
    """
    from services.loyalty_engine import is_loyalty_enabled
    if not await is_loyalty_enabled():
        return {"ok": False, "reason": "loyalty_disabled"}

    threshold = await get_system_number("LOYALTY_QR_COLLECTION_THRESHOLD", 10)
    reward_points = await get_system_number("LOYALTY_QR_COLLECTION_REWARD_POINTS", 200)
    rating_delta = await get_system_number("LOYALTY_QR_COLLECTION_RATING_DELTA", 5)

    if not threshold or not reward_points:
        return {"ok": False, "reason": "not_configured"}

    threshold = int(threshold)
    reward_points = int(reward_points)

    # Подсчитать прогресс QR-коллекции: сколько «обменов QR» реально зафиксировано.
    count = await _count_qr_collection_exchanges(owner_user_id)

    if count < threshold:
        return {
            "ok": False,
            "reason": "threshold_not_reached",
            "current": count,
            "threshold": threshold,
        }

    # Определить уровень награды (может быть несколько порогов)
    reward_level = count // threshold
    idempotency_key = f"qr_collection:{owner_user_id}:level:{reward_level}"

    lifetime_days = await get_system_number("LOYALTY_DEFAULT_POINTS_LIFETIME_DAYS", 365)

    result = await post_accrual(
        owner_type="client",
        owner_id=owner_user_id,
        amount=reward_points,
        event_type="qr_collection",
        idempotency_key=idempotency_key,
        description=f"Награда за {reward_level * threshold} собранных QR-обменов!",
        rating_delta=int(rating_delta) if rating_delta is not None else 5,
        lifetime_days=int(lifetime_days) if lifetime_days else 365,
        meta={"level": reward_level, "count": count, "threshold": threshold},
    )

    return result


async def get_referral_progress(owner_user_id: int) -> Dict[str, Any]:
    """Прогресс реферальной программы: собрано / порог / награды."""
    threshold = await get_system_number("LOYALTY_QR_COLLECTION_THRESHOLD", 10)
    threshold = int(threshold) if threshold else 10

    count = await _count_qr_collection_exchanges(owner_user_id)
    rewards_earned = count // threshold if threshold > 0 else 0
    next_threshold = (rewards_earned + 1) * threshold
    remaining = max(0, next_threshold - count)

    return {
        "scanned": count,
        "threshold": threshold,
        "rewards_earned": rewards_earned,
        "remaining_to_next": remaining,
    }


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

async def _find_referrer(invited_user_id: int) -> int | None:
    """
    Находит реферера по invited_user_id.
    Ищет через referral_activations → referral_codes → owner.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT rc.owner_user_id AS referrer_id
                FROM referral_activations ra
                JOIN referral_codes rc ON rc.id = ra.referral_code_id
                WHERE ra.user_id = %s
                  AND rc.owner_user_id IS NOT NULL
                LIMIT 1
                """,
                (str(invited_user_id),),
            )
            row = await c.fetchone()
            return int(row["referrer_id"]) if row else None




async def register_qr_collection_exchange(owner_user_id: int, invited_user_id: int) -> bool:
    """
    Зафиксировать «обмен QR»: owner_user_id перешёл по QR приглашённого invited_user_id.

    Условия зачёта:
    - invited_user_id действительно приглашён owner_user_id (по referral_activations);
    - invited_user_id верифицирован (users.is_verified=1).
    """
    if owner_user_id <= 0 or invited_user_id <= 0 or owner_user_id == invited_user_id:
        return False

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT 1
                FROM referral_activations ra
                JOIN referral_codes rc ON rc.id = ra.referral_code_id
                JOIN users u ON u.id = CAST(ra.user_id AS UNSIGNED)
                WHERE rc.owner_user_id = %s
                  AND ra.user_id = %s
                  AND COALESCE(u.is_verified, 0) = 1
                LIMIT 1
                """,
                (int(owner_user_id), str(int(invited_user_id))),
            )
            if not await c.fetchone():
                return False

            await c.execute(
                """
                INSERT IGNORE INTO referral_qr_collection (owner_user_id, invited_user_id)
                VALUES (%s, %s)
                """,
                (int(owner_user_id), int(invited_user_id)),
            )
            await _maybe_await(conn.commit())
            return c.rowcount > 0


async def _count_qr_collection_exchanges(owner_user_id: int) -> int:
    """Количество зафиксированных QR-обменов для владельца."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM referral_qr_collection
                WHERE owner_user_id = %s
                """,
                (int(owner_user_id),),
            )
            row = await c.fetchone()
            return int((row or {}).get("cnt") or 0)
