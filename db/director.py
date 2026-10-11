from __future__ import annotations
from typing import Any, Dict, List, Optional
from db.connection import get_conn, _cur, _maybe_await


# ===========================================================================
# ИНФОРМАЦИЯ О ЗАКАЗАХ ДЛЯ ДИРЕКТОРА
# ===========================================================================

###информация о заказах для директора
async def get_users_with_orders():
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT DISTINCT
                  u.id, first_name
                FROM users u
                LEFT JOIN orders o ON u.id=o.user_id
                WHERE o.status="CONFIRMED"
                  AND COALESCE(o.source, '') != 'manual'
                """
            )
            return await c.fetchall() or []

async def get_orders_of_user(user_id: int):
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  o.id, o.created_at
                FROM orders o
                WHERE user_id=%s
                  AND COALESCE(o.source, '') != 'manual'
                """,
                (user_id,),
            )
            return await c.fetchall() or []

async def get_order_items_equipment(order_id: int):
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  eq.name, eq.is_used, oi.qty
                FROM order_items oi
                INNER JOIN equipment eq ON oi.equipment_id=eq.id
                WHERE order_id=%s
                """,
                (order_id,),
            )
            return await c.fetchall() or []


async def get_order_items_codes(order_id: int) -> List[Dict[str, Any]]:
    """Коды, заказанные в рамках services_for_orders (ручная выдача директором).

    Возвращает строки:
      order_item_id, code_id, qty, vendor_name, operator_id, type_id,
      months_number, code_value, issued_at
    """
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return []

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  oi.id AS order_item_id,
                  oi.qty AS qty,
                  cd.id AS code_id,
                  cd.vendor_name,
                  cd.operator_id,
                  cd.type_id,
                  cd.months_number,
                  cd.code_value,
                  cd.issued_at
                FROM order_items oi
                JOIN codes cd ON cd.id = oi.code_id
                WHERE oi.order_id=%s AND cd.code_type='service_order'
                ORDER BY oi.id ASC
                """,
                (oid,),
            )
            return await c.fetchall() or []


async def set_service_order_code_value(code_id: int, code_value: str) -> bool:
    """Директор вручную вставляет код для services_for_orders.

    Заполняем codes.code_value и ставим issued_at. Если months_number задан — считаем expires_at.
    """
    try:
        cid = int(code_id)
    except (ValueError, TypeError):
        return False

    val = (code_value or "").strip()
    if not val:
        return False

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT months_number FROM codes WHERE id=%s AND code_type='service_order' LIMIT 1", (cid,))
            row = await c.fetchone()
            mn = None
            try:
                mn = int((row or {}).get("months_number")) if row and row.get("months_number") is not None else None
            except Exception:
                mn = None

            if mn and mn > 0:
                await c.execute(
                    """
                    UPDATE codes
                       SET code_value=%s,
                           issued_at=UTC_TIMESTAMP(),
                           expires_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MONTH)
                     WHERE id=%s AND code_type='service_order'
                    """,
                    (val, mn, cid),
                )
            else:
                await c.execute(
                    """
                    UPDATE codes
                       SET code_value=%s,
                           issued_at=UTC_TIMESTAMP()
                     WHERE id=%s AND code_type='service_order'
                    """,
                    (val, cid),
                )
            await _maybe_await(conn.commit())
            return c.rowcount == 1

async def set_order_finished(order_id: int):
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE orders SET status="SHIPPED" WHERE id = %s;
                """,
                (order_id,)
            )
            await _maybe_await(conn.commit())
