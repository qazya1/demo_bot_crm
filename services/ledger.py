"""
Атомарная работа с bonus-ledger.

Все финансовые операции с баллами проходят через этот модуль.
Гарантирует идемпотентность (по idempotency_key), корректное
обновление материализованного баланса и append-only принцип.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from db.connection import get_conn, _cur, _maybe_await, _begin_tx
from db.loyalty import (
    ensure_loyalty_account,
    insert_loyalty_transaction,
    get_transaction_by_idempotency_key,
    list_transactions as db_list_transactions,
    list_expiring_transactions,
    mark_transaction_expired,
    get_balance,
    update_balance_credit,
    update_balance_reserve,
    update_balance_redeem,
    update_balance_release,
    update_balance_debit,
    rebuild_balance as db_rebuild_balance,
    ensure_user_loyalty_profile,
    update_user_loyalty_profile,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Account management
# ─────────────────────────────────────────────────────────────

async def ensure_account(owner_type: str, owner_id: int) -> int:
    """Возвращает account_id, создавая при необходимости."""
    return await ensure_loyalty_account(owner_type, owner_id)


# ─────────────────────────────────────────────────────────────
# Accrual (начисление)
# ─────────────────────────────────────────────────────────────

async def post_accrual(
    owner_type: str,
    owner_id: int,
    amount: int,
    event_type: str,
    idempotency_key: str,
    *,
    description: str | None = None,
    rating_delta: int = 0,
    expires_at: datetime | None = None,
    lifetime_days: int | None = None,
    ref_entity_type: str | None = None,
    ref_entity_id: int | None = None,
    meta: dict | None = None,
    notify_client: bool = True,
) -> Dict[str, Any]:
    """
    Начислить баллы.

    Возвращает {"ok": True, "transaction": {...}, "duplicate": False}
    или {"ok": True, "duplicate": True} если ключ уже использован.
    """
    if amount <= 0:
        return {"ok": False, "error": "amount must be positive"}

    # Вычислить expires_at из lifetime_days
    if expires_at is None and lifetime_days and lifetime_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=lifetime_days)

    account_id = await ensure_account(owner_type, owner_id)

    # Атомарно: вставить транзакцию + обновить баланс
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            tx = await insert_loyalty_transaction(
                account_id=account_id,
                direction="credit",
                amount=amount,
                event_type=event_type,
                idempotency_key=idempotency_key,
                description=description,
                rating_delta=rating_delta,
                expires_at=expires_at,
                ref_entity_type=ref_entity_type,
                ref_entity_id=ref_entity_id,
                meta=meta,
                conn=conn,
                cursor=c,
            )

            if tx is None:
                await _maybe_await(conn.commit())
                log.info("Duplicate accrual key=%s", idempotency_key)
                return {"ok": True, "duplicate": True}

            await update_balance_credit(account_id, amount, conn=conn, cursor=c)
            await _maybe_await(conn.commit())

    if owner_type == "client" and rating_delta:
        try:
            await ensure_user_loyalty_profile(int(owner_id))
            await update_user_loyalty_profile(int(owner_id), rating_delta=int(rating_delta))
        except Exception:
            log.warning("Failed to update loyalty profile rating: user=%s", owner_id, exc_info=True)

    if owner_type == "client" and notify_client:
        try:
            from services.loyalty_notifications import notify_client_points_accrual
            await notify_client_points_accrual(
                int(owner_id),
                amount=int(amount),
                event_type=event_type,
                description=description,
            )
        except Exception:
            log.warning("Failed to notify client about accrual: user=%s", owner_id, exc_info=True)

    log.info(
        "Accrual: owner=%s:%s amount=%d type=%s key=%s",
        owner_type, owner_id, amount, event_type, idempotency_key,
    )
    return {"ok": True, "transaction": tx, "duplicate": False}


# ─────────────────────────────────────────────────────────────
# Reserve / Redeem / Release (для оплаты баллами)
# ─────────────────────────────────────────────────────────────

async def reserve_points(
    owner_type: str,
    owner_id: int,
    amount: int,
    idempotency_key: str,
    *,
    description: str | None = None,
    ref_entity_type: str | None = None,
    ref_entity_id: int | None = None,
    meta: dict | None = None,
) -> Dict[str, Any]:
    """
    Зарезервировать баллы (available → reserved).
    Возвращает {"ok": True/False, ...}
    """
    if amount <= 0:
        return {"ok": False, "error": "amount must be positive"}

    account_id = await ensure_account(owner_type, owner_id)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            # Проверка доступного баланса
            await c.execute(
                "SELECT available_amount FROM loyalty_balances WHERE account_id = %s FOR UPDATE",
                (account_id,),
            )
            bal = await c.fetchone()
            if not bal or bal["available_amount"] < amount:
                await _maybe_await(conn.commit())
                return {"ok": False, "error": "insufficient_balance",
                        "available": bal["available_amount"] if bal else 0}

            tx = await insert_loyalty_transaction(
                account_id=account_id,
                direction="debit",
                amount=amount,
                event_type="reserve",
                idempotency_key=idempotency_key,
                description=description or "Резерв баллов для оплаты",
                ref_entity_type=ref_entity_type,
                ref_entity_id=ref_entity_id,
                meta=meta,
                conn=conn,
                cursor=c,
            )

            if tx is None:
                await _maybe_await(conn.commit())
                return {"ok": True, "duplicate": True}

            await update_balance_reserve(account_id, amount, conn=conn, cursor=c)
            await _maybe_await(conn.commit())

    log.info("Reserve: owner=%s:%s amount=%d key=%s", owner_type, owner_id, amount, idempotency_key)
    return {"ok": True, "transaction": tx, "duplicate": False}


async def redeem_reserved_points(
    reservation_idempotency_key: str,
) -> Dict[str, Any]:
    """
    Подтвердить списание зарезервированных баллов (reserved → spent).
    """
    original = await get_transaction_by_idempotency_key(reservation_idempotency_key)
    if not original:
        return {"ok": False, "error": "reservation_not_found"}

    redeem_key = f"redeem:{reservation_idempotency_key}"
    existing = await get_transaction_by_idempotency_key(redeem_key)
    if existing:
        return {"ok": True, "duplicate": True}

    account_id = original["account_id"]
    amount = original["amount"]

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            tx = await insert_loyalty_transaction(
                account_id=account_id,
                direction="debit",
                amount=amount,
                event_type="redeem",
                idempotency_key=redeem_key,
                description="Списание баллов (оплата подтверждена)",
                ref_entity_type=original.get("ref_entity_type"),
                ref_entity_id=original.get("ref_entity_id"),
                conn=conn,
                cursor=c,
            )

            if tx is None:
                await _maybe_await(conn.commit())
                return {"ok": True, "duplicate": True}

            await update_balance_redeem(account_id, amount, conn=conn, cursor=c)
            await _maybe_await(conn.commit())

    log.info("Redeem: key=%s amount=%d", reservation_idempotency_key, amount)
    return {"ok": True, "transaction": tx}


async def release_reserved_points(
    reservation_idempotency_key: str,
) -> Dict[str, Any]:
    """
    Вернуть зарезервированные баллы (reserved → available, отмена оплаты).
    """
    original = await get_transaction_by_idempotency_key(reservation_idempotency_key)
    if not original:
        return {"ok": False, "error": "reservation_not_found"}

    release_key = f"release:{reservation_idempotency_key}"
    existing = await get_transaction_by_idempotency_key(release_key)
    if existing:
        return {"ok": True, "duplicate": True}

    account_id = original["account_id"]
    amount = original["amount"]

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            tx = await insert_loyalty_transaction(
                account_id=account_id,
                direction="credit",
                amount=amount,
                event_type="release",
                idempotency_key=release_key,
                description="Возврат зарезервированных баллов",
                reversal_of_transaction_id=original["id"],
                ref_entity_type=original.get("ref_entity_type"),
                ref_entity_id=original.get("ref_entity_id"),
                conn=conn,
                cursor=c,
            )

            if tx is None:
                await _maybe_await(conn.commit())
                return {"ok": True, "duplicate": True}

            await update_balance_release(account_id, amount, conn=conn, cursor=c)
            await _maybe_await(conn.commit())

    log.info("Release: key=%s amount=%d", reservation_idempotency_key, amount)
    return {"ok": True, "transaction": tx}


# ─────────────────────────────────────────────────────────────
# Expiration (сгорание баллов)
# ─────────────────────────────────────────────────────────────

async def expire_transactions(
    batch_size: int = 500,
    as_of: datetime | None = None,
) -> int:
    """
    Обработать сгоревшие баллы пакетом.
    Для каждой просроченной credit-транзакции создаёт debit-сторно.
    Возвращает количество обработанных.
    """
    expired_rows = await list_expiring_transactions(batch_size, as_of)
    count = 0

    # Не даём сгоранию задевать уже фактически списанные баллы:
    # если доступного баланса меньше суммы пакета, пакет пропускаем.
    # Это исключает кейс «списал баллы, но позже они отмечены как истёкшие».
    available_by_account: dict[int, int] = {}

    for row in expired_rows:
        expire_key = f"expiry:tx:{row['id']}"
        existing = await get_transaction_by_idempotency_key(expire_key)
        if existing:
            await mark_transaction_expired(row["id"])
            count += 1
            continue

        account_id = int(row["account_id"])
        amount = int(row["amount"])

        if account_id not in available_by_account:
            bal = await get_balance(account_id)
            available_by_account[account_id] = int((bal or {}).get("available_amount") or 0)

        if available_by_account[account_id] < amount:
            continue

        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await _maybe_await(_begin_tx(conn))

                tx = await insert_loyalty_transaction(
                    account_id=account_id,
                    direction="debit",
                    amount=amount,
                    event_type="expiry",
                    idempotency_key=expire_key,
                    description="Сгорание баллов по сроку",
                    reversal_of_transaction_id=row["id"],
                    conn=conn,
                    cursor=c,
                )

                if tx:
                    await update_balance_debit(account_id, amount, conn=conn, cursor=c)
                    await mark_transaction_expired(row["id"], conn=conn, cursor=c)
                    available_by_account[account_id] -= amount

                await _maybe_await(conn.commit())
        count += 1

    if count:
        log.info("Expired %d transactions", count)
    return count


# ─────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────

async def get_available_balance(owner_type: str, owner_id: int) -> int:
    """Текущий доступный баланс (0 если счёта нет)."""
    from db.loyalty import get_loyalty_account

    acc = await get_loyalty_account(owner_type, owner_id)
    if not acc:
        return 0
    bal = await get_balance(acc["id"])
    return bal["available_amount"] if bal else 0


async def list_account_transactions(
    owner_type: str,
    owner_id: int,
    limit: int = 20,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Транзакции по owner_type + owner_id."""
    from db.loyalty import get_loyalty_account

    acc = await get_loyalty_account(owner_type, owner_id)
    if not acc:
        return []
    return await db_list_transactions(acc["id"], limit, offset)


async def rebuild_balance(account_id: int) -> Dict[str, int]:
    """Пересборка баланса из ledger."""
    return await db_rebuild_balance(account_id)
