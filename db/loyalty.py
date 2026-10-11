"""
CRUD-операции для таблиц клиентской лояльности:
- loyalty_accounts
- loyalty_transactions
- loyalty_balances
- user_loyalty_profile
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from db.connection import (
    get_conn,
    _cur,
    _maybe_await,
    _begin_tx,
    IntegrityError,
)


# ─────────────────────────────────────────────────────────────
# loyalty_accounts
# ─────────────────────────────────────────────────────────────

async def ensure_loyalty_account(
    owner_type: str,
    owner_id: int,
) -> int:
    """Возвращает account_id. Создаёт запись + баланс, если ещё нет."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id FROM loyalty_accounts
                WHERE owner_type = %s AND owner_id = %s
                LIMIT 1
                """,
                (owner_type, owner_id),
            )
            row = await c.fetchone()
            if row:
                return row["id"]

            await c.execute(
                """
                INSERT INTO loyalty_accounts (owner_type, owner_id)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE updated_at = NOW()
                """,
                (owner_type, owner_id),
            )
            account_id = c.lastrowid

            # Создаём строку баланса
            await c.execute(
                """
                INSERT IGNORE INTO loyalty_balances (account_id)
                VALUES (%s)
                """,
                (account_id,),
            )
            await _maybe_await(conn.commit())
            return account_id


async def get_loyalty_account(owner_type: str, owner_id: int) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, owner_type, owner_id, is_active, created_at, updated_at
                FROM loyalty_accounts
                WHERE owner_type = %s AND owner_id = %s
                LIMIT 1
                """,
                (owner_type, owner_id),
            )
            return await c.fetchone()


# ─────────────────────────────────────────────────────────────
# loyalty_transactions (ledger)
# ─────────────────────────────────────────────────────────────

async def insert_loyalty_transaction(
    account_id: int,
    direction: str,
    amount: int,
    event_type: str,
    idempotency_key: str,
    *,
    description: str | None = None,
    rating_delta: int = 0,
    reversal_of_transaction_id: int | None = None,
    expires_at: datetime | None = None,
    ref_entity_type: str | None = None,
    ref_entity_id: int | None = None,
    meta: dict | None = None,
    conn=None,
    cursor=None,
) -> Optional[Dict[str, Any]]:
    """
    Вставляет запись в ledger.
    Возвращает None если idempotency_key уже существует (дубликат).
    Если передан conn/cursor — использует их (для атомарных транзакций).
    """
    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None

    async def _do(c):
        try:
            await c.execute(
                """
                INSERT INTO loyalty_transactions
                    (account_id, direction, amount, event_type, idempotency_key,
                     description, rating_delta, reversal_of_transaction_id,
                     expires_at, ref_entity_type, ref_entity_id, meta_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    account_id, direction, amount, event_type, idempotency_key,
                    description, rating_delta, reversal_of_transaction_id,
                    expires_at, ref_entity_type, ref_entity_id, meta_json,
                ),
            )
            tx_id = c.lastrowid
            await c.execute(
                "SELECT * FROM loyalty_transactions WHERE id = %s", (tx_id,)
            )
            return await c.fetchone()
        except IntegrityError as e:
            if "uq_loyalty_tx_idempotency" in str(e) or "Duplicate" in str(e):
                return None
            raise

    if cursor:
        return await _do(cursor)

    async with get_conn() as cn:
        async with await _cur(cn) as c:
            result = await _do(c)
            if result:
                await _maybe_await(cn.commit())
            return result


async def get_transaction_by_idempotency_key(key: str) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT * FROM loyalty_transactions WHERE idempotency_key = %s LIMIT 1",
                (key,),
            )
            return await c.fetchone()


async def list_transactions(
    account_id: int,
    limit: int = 20,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM loyalty_transactions
                WHERE account_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (account_id, limit, offset),
            )
            return await c.fetchall() or []


async def list_expiring_transactions(
    batch_size: int = 500,
    as_of: datetime | None = None,
) -> List[Dict[str, Any]]:
    """Возвращает транзакции с истёкшим сроком, ещё не помеченные."""
    as_of = as_of or datetime.utcnow()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM loyalty_transactions
                WHERE is_expired = 0
                  AND expires_at IS NOT NULL
                  AND expires_at <= %s
                  AND direction = 'credit'
                ORDER BY expires_at ASC
                LIMIT %s
                """,
                (as_of, batch_size),
            )
            return await c.fetchall() or []


async def mark_transaction_expired(tx_id: int, *, conn=None, cursor=None) -> None:
    async def _do(c):
        await c.execute(
            "UPDATE loyalty_transactions SET is_expired = 1 WHERE id = %s",
            (tx_id,),
        )

    if cursor:
        return await _do(cursor)

    async with get_conn() as cn:
        async with await _cur(cn) as c:
            await _do(c)
            await _maybe_await(cn.commit())


# ─────────────────────────────────────────────────────────────
# loyalty_balances
# ─────────────────────────────────────────────────────────────

async def get_balance(account_id: int) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT * FROM loyalty_balances WHERE account_id = %s LIMIT 1",
                (account_id,),
            )
            return await c.fetchone()


async def get_loyalty_balance(owner_type: str, owner_id: int) -> Optional[Dict[str, Any]]:
    """
    Получить баланс лояльности по owner_type и owner_id.
    Удобная функция для получения баланса без необходимости знать account_id.
    
    Args:
        owner_type: Тип владельца ('client', 'partner', 'specialist')
        owner_id: ID владельца (users.id)
    
    Returns:
        Словарь с балансом или None, если аккаунт не существует
    """
    account = await get_loyalty_account(owner_type, owner_id)
    if not account:
        return None
    
    return await get_balance(account['id'])


async def update_balance_credit(
    account_id: int, amount: int, *, conn=None, cursor=None
) -> None:
    """Прибавить к доступному балансу (начисление)."""
    async def _do(c):
        await c.execute(
            """
            UPDATE loyalty_balances
            SET available_amount = available_amount + %s,
                total_earned = total_earned + %s
            WHERE account_id = %s
            """,
            (amount, amount, account_id),
        )

    if cursor:
        return await _do(cursor)

    async with get_conn() as cn:
        async with await _cur(cn) as c:
            await _do(c)
            await _maybe_await(cn.commit())


async def update_balance_reserve(
    account_id: int, amount: int, *, conn=None, cursor=None
) -> None:
    """Зарезервировать баллы (available → reserved)."""
    async def _do(c):
        await c.execute(
            """
            UPDATE loyalty_balances
            SET available_amount = available_amount - %s,
                reserved_amount = reserved_amount + %s
            WHERE account_id = %s AND available_amount >= %s
            """,
            (amount, amount, account_id, amount),
        )
        return c.rowcount

    if cursor:
        return await _do(cursor)

    async with get_conn() as cn:
        async with await _cur(cn) as c:
            result = await _do(c)
            await _maybe_await(cn.commit())
            return result


async def update_balance_redeem(
    account_id: int, amount: int, *, conn=None, cursor=None
) -> None:
    """Списать зарезервированные баллы (reserved → spent)."""
    async def _do(c):
        await c.execute(
            """
            UPDATE loyalty_balances
            SET reserved_amount = reserved_amount - %s,
                total_spent = total_spent + %s
            WHERE account_id = %s AND reserved_amount >= %s
            """,
            (amount, amount, account_id, amount),
        )

    if cursor:
        return await _do(cursor)

    async with get_conn() as cn:
        async with await _cur(cn) as c:
            await _do(c)
            await _maybe_await(cn.commit())


async def update_balance_release(
    account_id: int, amount: int, *, conn=None, cursor=None
) -> None:
    """Вернуть зарезервированные баллы (reserved → available)."""
    async def _do(c):
        await c.execute(
            """
            UPDATE loyalty_balances
            SET reserved_amount = reserved_amount - %s,
                available_amount = available_amount + %s
            WHERE account_id = %s
            """,
            (amount, amount, account_id),
        )

    if cursor:
        return await _do(cursor)

    async with get_conn() as cn:
        async with await _cur(cn) as c:
            await _do(c)
            await _maybe_await(cn.commit())


async def update_balance_debit(
    account_id: int, amount: int, *, conn=None, cursor=None
) -> None:
    """Прямое списание из доступного баланса (сгорание, штраф)."""
    async def _do(c):
        await c.execute(
            """
            UPDATE loyalty_balances
            SET available_amount = GREATEST(available_amount - %s, 0),
                total_spent = total_spent + %s
            WHERE account_id = %s
            """,
            (amount, amount, account_id),
        )

    if cursor:
        return await _do(cursor)

    async with get_conn() as cn:
        async with await _cur(cn) as c:
            await _do(c)
            await _maybe_await(cn.commit())


async def rebuild_balance(account_id: int) -> Dict[str, int]:
    """Пересборка баланса из ledger (восстановительная команда)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction='credit' AND is_expired=0
                                      AND reversal_of_transaction_id IS NULL
                                 THEN amount ELSE 0 END), 0) AS total_earned,
                    COALESCE(SUM(CASE WHEN direction='debit'
                                      AND reversal_of_transaction_id IS NULL
                                 THEN amount ELSE 0 END), 0) AS total_spent
                FROM loyalty_transactions
                WHERE account_id = %s
                """,
                (account_id,),
            )
            row = await c.fetchone()
            earned = int(row["total_earned"])
            spent = int(row["total_spent"])
            available = earned - spent

            await c.execute(
                """
                UPDATE loyalty_balances
                SET available_amount = %s,
                    reserved_amount = 0,
                    total_earned = %s,
                    total_spent = %s
                WHERE account_id = %s
                """,
                (available, earned, spent, account_id),
            )
            await _maybe_await(conn.commit())
            return {"available": available, "earned": earned, "spent": spent}


# ─────────────────────────────────────────────────────────────
# user_loyalty_profile
# ─────────────────────────────────────────────────────────────

async def ensure_user_loyalty_profile(user_id: int) -> Dict[str, Any]:
    """Создаёт профиль лояльности, если ещё нет. Возвращает текущий."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT IGNORE INTO user_loyalty_profile (user_id)
                VALUES (%s)
                """,
                (user_id,),
            )
            await _maybe_await(conn.commit())

            await c.execute(
                "SELECT * FROM user_loyalty_profile WHERE user_id = %s LIMIT 1",
                (user_id,),
            )
            return await c.fetchone()


async def get_user_loyalty_profile(user_id: int) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT * FROM user_loyalty_profile WHERE user_id = %s LIMIT 1",
                (user_id,),
            )
            return await c.fetchone()


async def update_user_loyalty_profile(
    user_id: int,
    *,
    rating_delta: int = 0,
    tier: str | None = None,
    orders_delta: int = 0,
    spent_rub_delta: float = 0,
    touch_activity: bool = True,
) -> None:
    """Инкрементальное обновление профиля."""
    parts = []
    params = []

    if rating_delta:
        parts.append("rating = rating + %s")
        params.append(rating_delta)
    if tier is not None:
        parts.append("tier = %s")
        params.append(tier)
    if orders_delta:
        parts.append("total_orders = total_orders + %s")
        params.append(orders_delta)
    if spent_rub_delta:
        parts.append("total_spent_rub = total_spent_rub + %s")
        params.append(spent_rub_delta)
    if touch_activity:
        parts.append("last_activity_at = NOW()")

    if not parts:
        return

    params.append(user_id)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"UPDATE user_loyalty_profile SET {', '.join(parts)} WHERE user_id = %s",
                tuple(params),
            )
            await _maybe_await(conn.commit())


async def list_inactive_profiles(
    inactivity_days: int = 180,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Профили, у которых last_activity_at старше inactivity_days дней."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT user_id AS internal_user_id, rating, tier, last_activity_at
                FROM user_loyalty_profile
                WHERE last_activity_at IS NOT NULL
                  AND last_activity_at < DATE_SUB(NOW(), INTERVAL %s DAY)
                ORDER BY last_activity_at
                LIMIT %s
                """,
                (inactivity_days, limit),
            )
            return await c.fetchall() or []
