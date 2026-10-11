from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import logging
from db.connection import (
    get_conn, _cur, _maybe_await, _begin_tx, _rollback_if_needed,
    _add_months_utc,
    IntegrityError,
)

logger = logging.getLogger(__name__)


async def _resolve_order_user_id(
    *,
    internal_user_id: int | None = None,
    tg_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
    create: bool = False,
) -> int | None:
    from db.users import resolve_internal_user_id

    return await resolve_internal_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=tg_user_id,
        max_user_id=max_user_id,
        create=create,
    )


# =========================================================
# Orders (header)
# В новой схеме:
# - order_id (varchar) удалён, используем id (int) как PK
# - user_id теперь int (FK на users.id)
# - Удалены buyer_name, buyer_phone, tg_username, buyer_email, code_id, item_type, item_title
# =========================================================
async def create_order(
    user_id: str,
    amount: int,
    buyer_name: str | None = None,
    buyer_phone: str | None = None,
    tg_username: str | None = None,
    buyer_email: str | None = None,
    internal_user_id: int | None = None,
    max_user_id: int | str | None = None,
    source: str | None = None,
    address: str | None = None,
) -> int | None:
    """
    Создаёт заказ.
    Возвращает id созданного заказа.
    user_id - это telegram user_id, конвертируем во внутренний id.
    Если передан internal_user_id — используем его напрямую (для MAX-бота).
    source — источник заказа: 'telegram', 'max' и т.д.
    address — адрес доставки, введённый при оформлении.
    """
    internal_user_id = await _resolve_order_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=user_id or None,
        max_user_id=max_user_id,
    )
    if not internal_user_id:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO orders (user_id, amount, status, source, address, created_at, updated_at)
                VALUES (%s, %s, 'INIT', %s, %s, NOW(), NOW())
                """,
                (internal_user_id, int(amount), source, address),
            )
            await _maybe_await(conn.commit())
            return int(c.lastrowid)


async def create_order_from_chat_payment(
    *,
    user_id: int,
    specialist_id: int | None,
    amount: int,
    payment_id: str,
    source: str = "chat",
    description: str | None = None,
) -> int | None:
    """
    Создаёт заказ (сделку) из оплаты в чате поддержки.
    Гарантирует наличие колонок specialist_id/company_id.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Убедимся что колонки существуют
            for col, after in [("company_id", "user_id"), ("specialist_id", "company_id")]:
                await c.execute(f"SHOW COLUMNS FROM orders LIKE '{col}'")
                if not await c.fetchone():
                    await c.execute(
                        f"ALTER TABLE orders ADD COLUMN {col} INT DEFAULT NULL AFTER {after}"
                    )

            await c.execute(
                """
                INSERT INTO orders
                    (user_id, specialist_id, amount, status, source, payment_id, description, created_at, updated_at, paid_at)
                VALUES
                    (%s, %s, %s, 'CONFIRMED', %s, %s, %s, NOW(), NOW(), NOW())
                """,
                (int(user_id), (int(specialist_id) if specialist_id else None), int(amount), source, str(payment_id), description),
            )
            await _maybe_await(conn.commit())
            created_order_id = int(c.lastrowid)

    # Best-effort: доп. синхронизация downstream (уведомления/задачи CRM).
    try:
        await _dispatch_sale_to_crm(created_order_id, source_hint=source)
    except Exception:
        logger.warning(
            "CRM sale dispatch failed for chat payment order_id=%s payment_id=%s",
            created_order_id,
            payment_id,
            exc_info=True,
        )
    return created_order_id


async def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    """
    Получить заказ по id.
    order_id может быть как старый строковый, так и новый числовой.
    """
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT * FROM orders WHERE id=%s LIMIT 1", (oid,))
            row = await c.fetchone()
            if row:
                # Добавляем order_id для обратной совместимости
                row["order_id"] = str(row.get("id"))
            return row


async def set_order_status(order_id: str, status: str, payment_info: str | None = None) -> None:
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE orders
                   SET status=%s,
                       payment_info=COALESCE(%s, payment_info),
                       paid_at = CASE WHEN %s='CONFIRMED' THEN COALESCE(paid_at, NOW()) ELSE paid_at END,
                       updated_at=NOW()
                 WHERE id=%s
                """,
                (status, payment_info, status, oid),
            )
            await _maybe_await(conn.commit())


async def set_order_payment_id(order_id: str, payment_id: str) -> None:
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "UPDATE orders SET payment_id=%s, updated_at=NOW() WHERE id=%s",
                (payment_id, oid),
            )
            await _maybe_await(conn.commit())


async def get_order_by_payment_id(payment_id: str) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT * FROM orders WHERE payment_id=%s LIMIT 1", (payment_id,))
            row = await c.fetchone()
            if row:
                row["order_id"] = str(row.get("id"))
            return row


async def list_orders_awaiting_payment(max_age_minutes: int = 30) -> List[Dict[str, Any]]:
    """
    Заказы со статусом INIT, у которых задан payment_id и
    created_at не старше max_age_minutes минут.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM orders
                 WHERE UPPER(status) = 'INIT'
                   AND payment_id IS NOT NULL
                   AND payment_id != ''
                   AND created_at >= NOW() - INTERVAL %s MINUTE
              ORDER BY created_at DESC
                """,
                (max_age_minutes,),
            )
            rows = await c.fetchall() or []
            for r in rows:
                r["order_id"] = str(r.get("id"))
            return rows


async def get_open_order(
    user_id: str = "",
    *,
    internal_user_id: int | None = None,
    max_user_id: int | str | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Получить открытый заказ пользователя.
    user_id - это telegram user_id.
    """
    internal_user_id = await _resolve_order_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=user_id or None,
        max_user_id=max_user_id,
    )
    if not internal_user_id:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT *
                  FROM orders
                 WHERE user_id=%s AND UPPER(status)='INIT'
              ORDER BY created_at DESC
                 LIMIT 1
                """,
                (internal_user_id,),
            )
            row = await c.fetchone()
            if row:
                row["order_id"] = str(row.get("id"))
            return row


async def set_order_item_meta(order_id: str, item_type: str, item_title: str) -> None:
    """
    Легаси-функция. В новой схеме эти поля удалены из orders.
    Информация о товарах хранится в order_items.
    Сейчас это no-op для обратной совместимости.
    """
    pass  # В новой схеме эти поля не существуют


# =========================================================
# Order items (строки заказа)
# В новой схеме:
# - order_id теперь int (FK на orders.id)
# - code_id - связь с codes
# - Удалены: item_type, item_title, requires_code, code_type
# - Добавлены: legacy_item_type, legacy_item_title
# =========================================================
async def clear_order_items(order_id: str) -> None:
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("DELETE FROM order_items WHERE order_id=%s", (oid,))
            await _maybe_await(conn.commit())


async def save_order_items(order_id: str, items: List[Dict[str, Any]]) -> None:
    """
    Вставляем строки заказа.
    В новой схеме:
    - order_id - int
    - code_id, equipment_id - FK
    """
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            for it in (items or []):
                await c.execute(
                    """
                    INSERT INTO order_items (order_id, code_id, equipment_id, qty, price, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        oid,
                        it.get("code_id"),  # код будет привязан позже через issue_codes_for_order
                        (it.get("equipment_id") if it.get("equipment_id") is not None else (it.get("meta") or {}).get("equipment_id")),
                        int(it.get("qty") or 1),
                        int(it.get("price") or 0)
                    ),
                )
            await _maybe_await(conn.commit())


async def get_order_items(order_id: str) -> List[Dict[str, Any]]:
    """
    Получить строки заказа.
    """
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT * FROM order_items oi LEFT JOIN equipment eq ON eq.id=oi.equipment_id LEFT JOIN codes c ON c.id=oi.code_id WHERE order_id=%s ORDER BY oi.id", (oid,))
            rows = await c.fetchall()
            # Добавляем совместимость
            for row in rows:
                # Определяем requires_code по наличию code_id
                row["requires_code"] = 1 if row.get("code_id") else 0
            return rows


# =========================================================
# Codes pool + выдача
# В новой схеме:
# - order_codes таблица удалена
# - Связь кодов с заказами через order_items.code_id
# - codes.user_id теперь int (FK на users.id)
# - codes.order_id удалён
# - codes.code_type теперь ENUM
# =========================================================
async def add_codes_bulk(codes: List[str], code_type: str = "ofd_first") -> int:
    """
    Добавить коды в пул.
    code_type должен быть одним из: ofd_first, ofd_platform, ofd_other, service, edo
    """
    rows = [c.strip() for c in (codes or []) if c and c.strip()]
    if not rows:
        return 0

    ct = (code_type or "").strip().lower()
    if ct not in ("ofd_first", "ofd_platform", "ofd_other", "service", "edo"):
        ct = "ofd_first"

    added = 0
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            for cv in rows:
                try:
                    await c.execute(
                        """
                        INSERT INTO codes (code_value, code_type, is_used)
                        VALUES (%s,%s,0)
                        ON DUPLICATE KEY UPDATE id=id
                        """,
                        (cv, ct),
                    )
                    if c.rowcount == 1:
                        added += 1
                except Exception:
                    pass
            await _maybe_await(conn.commit())
    return added


async def get_stats() -> Dict[str, int]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT COUNT(*) c FROM codes")
            total = int((await c.fetchone())["c"])
            await c.execute("SELECT COUNT(*) c FROM codes WHERE is_used=1")
            used = int((await c.fetchone())["c"])
            return {"total": total, "used": used, "free": total - used}


async def get_free_codes_count(code_type: str) -> int:
    """Сколько свободных (невыданных) кодов доступно в пуле `code_type`.
    Используем для защиты корзины от заказа количества больше, чем есть в наличии.
    """
    ct = (code_type or "").strip().lower()
    if not ct:
        return 0
    if ct not in ("ofd_first", "ofd_platform", "ofd_other", "service", "edo"):
        return 0

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT COUNT(*) c FROM codes WHERE is_used=0 AND code_type=%s",
                (ct,),
            )
            row = await c.fetchone()
            return int((row or {}).get("c") or 0)



async def reserve_pool_code_for_user(
    user_id: str = "",
    code_type: str = "",
    *,
    internal_user_id: int | None = None,
    max_user_id: int | str | None = None,
    operator_id: int | None = None,
    type_id: int | None = None,
    sell_price: int | None = None,
) -> int | None:
    """Резервирует (бронирует) 1 код из нашего пула и привязывает его к пользователю.
    Возвращает code_id или None, если коды закончились.

    Важно: это бронь ДО оплаты. issued_at/expires_at ставятся при CONFIRMED.
    """
    ct = (code_type or "").strip().lower()
    internal_user_id = await _resolve_order_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=user_id or None,
        max_user_id=max_user_id,
    )
    if not internal_user_id:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            if operator_id and type_id:
                await c.execute(
                    """
                    SELECT id
                      FROM codes
                     WHERE is_used=0 AND code_type=%s AND operator_id=%s AND type_id=%s
                     ORDER BY id
                     LIMIT 1
                     FOR UPDATE
                    """,
                    (ct, operator_id, type_id),
                )
            else:
                await c.execute(
                    """
                    SELECT id
                      FROM codes
                     WHERE is_used=0 AND code_type=%s
                     ORDER BY id
                     LIMIT 1
                     FOR UPDATE
                    """,
                    (ct,),
                )
            row = await c.fetchone()
            if not row:
                await _maybe_await(conn.rollback())
                return None

            code_id = int(row["id"])

            await c.execute(
                """
                UPDATE codes
                   SET is_used=1,
                       user_id=%s,
                       issued_at=NULL,
                       expires_at=NULL,
                       sell_price=COALESCE(%s, sell_price)
                 WHERE id=%s AND is_used=0
                """,
                (internal_user_id, sell_price, code_id),
            )
            if c.rowcount != 1:
                await _maybe_await(conn.rollback())
                return None

            await _maybe_await(conn.commit())
            return code_id


async def reserve_external_code_for_user(
    user_id: str = "",
    *,
    internal_user_id: int | None = None,
    max_user_id: int | str | None = None,
    code_type: str,
    operator_id: int | None,
    type_id: int | None,
    vseofd_order_id: str | None,
    sell_price: int | None = None,
    source_price: int | None = None,
    vendor_name: str | None = None,
    months_number: int | None = None,
) -> int | None:
    """Создаёт в `codes` забронированную запись под внешний код (ОФД/сервис/ЭДО).
    code_value создаём пустой строкой (поле NOT NULL). Значение пишем после оплаты.

    Возвращает code_id.
    """
    ct = (code_type or "").strip().lower()
    if ct not in ("ofd_other", "service", "edo", "service_order"):
        return None
    internal_user_id = await _resolve_order_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=user_id or None,
        max_user_id=max_user_id,
    )
    if not internal_user_id:
        return None

    mn = None
    try:
        mn = int(months_number) if months_number is not None else None
    except Exception:
        mn = None

    try:
        op = int(operator_id) if operator_id is not None else None
    except Exception:
        op = None
    try:
        tp = int(type_id) if type_id is not None else None
    except Exception:
        tp = None

    # vseofd_order_id - uid
    vo = None
    try:
        vo = vseofd_order_id if vseofd_order_id is not None and str(vseofd_order_id).strip() else None
    except Exception:
        vo = None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO codes (
                    code_value, is_used, user_id, issued_at,
                    code_type, operator_id, type_id, vseofd_order_id,
                    source_price, sell_price, vendor_name,
                    months_number, expires_at
                )
                VALUES (%s, 1, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                """,
                (
                    "",
                    internal_user_id,
                    ct,
                    op,
                    tp,
                    vo,
                    source_price,
                    sell_price,
                    vendor_name,
                    mn,
                ),
            )
            code_id = int(getattr(c, "lastrowid", 0) or 0)
            await _maybe_await(conn.commit())
            return code_id or None


async def rollback_order_reservations(order_id: str) -> None:
    """Откатывает брони по заказу (если клиент не оплатил).
    - Внутренние и неиспользованные внешние пуловые коды: снимаем бронь (is_used=0, user_id=NULL, issued_at/expires_at=NULL).
    - Свежие внешние коды: удаляем созданные заглушки (code_value='' AND issued_at IS NULL).
    """
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            await c.execute(
                """
                SELECT DISTINCT cd.id, cd.code_type, cd.code_value, cd.issued_at
                  FROM order_items oi
                  JOIN codes cd ON cd.id = oi.code_id
                 WHERE oi.order_id=%s AND oi.code_id IS NOT NULL
                """,
                (oid,),
            )
            rows = await c.fetchall() or []

            pool_ids = []
            ext_ids = []

            for r in rows:
                if r.get("issued_at") is not None:
                    continue  # уже выдано/зафиксировано
                ct = (r.get("code_type") or "").lower()
                cv = r.get("code_value") or ""
                cid = int(r.get("id") or 0)
                if not cid:
                    continue
                if cv:
                    pool_ids.append(cid)
                else:
                    # внешние и любые прочие "заглушки"
                    if (cv == "") and ct in ("ofd_other", "service", "edo"):
                        ext_ids.append(cid)

            if pool_ids:
                await c.execute(
                    f"""
                    UPDATE codes
                       SET is_used=0,
                           user_id=NULL,
                           issued_at=NULL,
                           expires_at=NULL,
                           sell_price=NULL
                     WHERE id IN ({','.join(['%s']*len(pool_ids))})
                       AND issued_at IS NULL
                    """,
                    tuple(pool_ids),
                )

            if ext_ids:
                await c.execute(
                    f"DELETE FROM codes WHERE id IN ({','.join(['%s']*len(ext_ids))}) AND issued_at IS NULL AND code_value=''" ,
                    tuple(ext_ids),
                )

            await _maybe_await(conn.commit())


async def confirm_order_paid(order_id: str, user_id: str, *, payment_info: str | None = None) -> bool:
    """Атомарно фиксирует оплату заказа.
    Возвращает True, если это было первое подтверждение (т.е. выполнены одноразовые действия):
    - финализация пуловых кодов (issued_at/expires_at)
    - списание остатка оборудования (equipment.count-- при count IS NOT NULL)
    """
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return False

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            await c.execute("SELECT status, paid_at FROM orders WHERE id=%s FOR UPDATE", (oid,))
            row = await c.fetchone() or {}
            prev_paid = row.get("paid_at")
            prev_status = str(row.get("status") or "").upper()

            # уже подтверждено ранее
            if prev_status == "CONFIRMED" and prev_paid is not None:
                await _maybe_await(conn.commit())
                return False

            await c.execute(
                """
                UPDATE orders
                   SET status='CONFIRMED',
                       payment_info=COALESCE(%s, payment_info),
                       paid_at=COALESCE(paid_at, NOW()),
                       updated_at=NOW()
                 WHERE id=%s
                """,
                (payment_info, oid),
            )

            first_time = prev_paid is None

            if first_time:
                # 1) списываем оборудование (count IS NOT NULL)
                await c.execute(
                    """
                    SELECT equipment_id
                      FROM order_items
                     WHERE order_id=%s AND equipment_id IS NOT NULL
                    """,
                    (oid,),
                )
                eq_rows = await c.fetchall() or []
                eq_ids = [int(r.get("equipment_id")) for r in eq_rows if r.get("equipment_id")]
                for eq_id in eq_ids:
                    await c.execute(
                        """
                        UPDATE equipment
                           SET count = CASE
                               WHEN count IS NULL THEN NULL
                               WHEN count > 0 THEN count - 1
                               ELSE 0
                           END
                         WHERE id=%s
                        """,
                        (eq_id,),
                    )

                # 2) финализируем пуловые коды (issued_at/expires)
                await c.execute(
                    """
                    UPDATE codes cd
                    JOIN order_items oi ON oi.code_id = cd.id
                       SET cd.issued_at = COALESCE(cd.issued_at, UTC_TIMESTAMP()),
                           cd.months_number = COALESCE(cd.months_number, 12),
                           cd.expires_at = COALESCE(cd.expires_at, DATE_ADD(UTC_TIMESTAMP(), INTERVAL COALESCE(cd.months_number, 12) MONTH))
                     WHERE oi.order_id=%s
                       AND cd.code_value IS NOT NULL
                       AND cd.is_used=1
                       AND cd.issued_at IS NULL
                    """,
                    (oid,),
                )

            # 3) связываем support_chat_payments.order_id по payment_id
            if first_time:
                try:
                    await c.execute(
                        "SELECT payment_id FROM orders WHERE id=%s LIMIT 1", (oid,)
                    )
                    pid_row = await c.fetchone()
                    pid = (pid_row or {}).get("payment_id")
                    if pid:
                        # Колонка может ещё не существовать — игнорируем ошибку
                        await c.execute(
                            """UPDATE support_chat_payments
                                  SET order_id = %s
                                WHERE payment_id = %s
                                  AND order_id IS NULL""",
                            (oid, pid),
                        )
                except Exception:
                    pass  # best-effort

            await _maybe_await(conn.commit())

    # ── Loyalty: начислить баллы за покупку (вне транзакции, чтобы не блокировать)
    if first_time:
        try:
            await _award_loyalty_for_order(oid, str(user_id))
        except Exception:
            logger.warning(
                "Loyalty award failed for order %s, user %s — не критично",
                oid, user_id, exc_info=True,
            )

    # ── Канонический post-payment dispatch в CRM (идемпотентный best-effort)
    try:
        await _dispatch_sale_to_crm(oid)
    except Exception:
        logger.warning("CRM sale dispatch failed for order %s", oid, exc_info=True)

    return bool(first_time)


async def _dispatch_sale_to_crm(order_id: int, *, source_hint: str | None = None) -> None:
    """Единая post-payment отправка sale-события в Django CRM (best-effort, идемпотентно на стороне CRM)."""
    from bot.django_crm import push_sale_to_crm
    from db.users import get_user

    order_row = await get_order(str(order_id))
    if not order_row:
        return

    status = str(order_row.get("status") or "").upper()
    if status != "CONFIRMED" or not order_row.get("paid_at"):
        return

    user_id = order_row.get("user_id")
    user = await get_user(internal_user_id=user_id) if user_id else {}
    source = str(source_hint or order_row.get("source") or "telegram").strip().lower()

    items = await get_order_items(str(order_id))
    detailed_codes = await get_order_codes_detailed(str(order_id))

    await push_sale_to_crm(
        order_id=str(order_id),
        total_rub=int(order_row.get("amount") or 0),
        cart=[],
        source=source,
        user_data={
            "first_name": (user or {}).get("first_name", ""),
            "last_name": (user or {}).get("last_name", ""),
            "phone": (user or {}).get("phone", ""),
            "email": (user or {}).get("email", ""),
            "inn": (user or {}).get("inn", ""),
            "city": (user or {}).get("city", ""),
            "tg_user_id": (user or {}).get("user_id", ""),
            "tg_username": (user or {}).get("tg_username", ""),
            "max_user_id": (user or {}).get("max_user_id", ""),
        },
        order_data=order_row,
        items=items,
        codes=None,
        detailed_codes=detailed_codes,
    )


async def ensure_order_post_payment_pipeline(order_id: str | int, *, source_hint: str | None = None) -> None:
    """Best-effort: прогнать канонический post-payment pipeline для уже существующего заказа.

    Используется как repair-хук, когда заказ уже есть, но нужно гарантировать downstream-эффекты
    (CRM sale -> задачи/уведомления в CRM стороне).
    """
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return

    await _dispatch_sale_to_crm(oid, source_hint=source_hint)


async def _award_loyalty_for_order(order_id: int, user_id: str) -> None:
    """Начислить баллы лояльности за оплаченный заказ (best-effort)."""
    from services.loyalty_engine import (
        is_loyalty_enabled,
        award_points_for_purchase,
        award_first_purchase_bonus,
    )
    if not await is_loyalty_enabled():
        return

    # Получить данные заказа
    order = await get_order(str(order_id))
    if not order:
        return

    internal_user_id = int(order.get("user_id") or 0)
    order_amount = int(order.get("amount") or 0)
    if not internal_user_id or order_amount <= 0:
        return

    # Начислить баллы за покупку
    await award_points_for_purchase(
        internal_user_id=internal_user_id,
        order_id=order_id,
        order_amount_rub=float(order_amount),
    )

    # Проверить, первая ли это покупка
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT COUNT(*) AS cnt FROM orders WHERE user_id=%s AND status='CONFIRMED'",
                (internal_user_id,),
            )
            row = await c.fetchone()
            if row and int(row["cnt"]) <= 1:
                await award_first_purchase_bonus(
                    internal_user_id=internal_user_id,
                    order_id=order_id,
                )


async def issue_code_for_order(order_id: str, user_id: str) -> Dict[str, List[str]]:
    """Возвращает коды, привязанные к заказу (order_items.code_id).

    В новой логике коды (и пуловые, и внешние) бронируются ДО оплаты:
    - Пуловые коды уже имеют code_value, но issued_at ставится при CONFIRMED (confirm_order_paid).
    - Внешние коды получают code_value после оплаты (process_external_ofd_after_payment).

    Эта функция больше НЕ выделяет новые коды из пула — только читает уже связанные.
    """
    result: Dict[str, List[str]] = {}

    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return result

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT cd.code_type, cd.code_value
                  FROM order_items oi
                  JOIN codes cd ON cd.id = oi.code_id
                 WHERE oi.order_id=%s AND oi.code_id IS NOT NULL
                 ORDER BY oi.id
                """,
                (oid,),
            )
            rows = await c.fetchall() or []
            for r in rows:
                ct = (r.get("code_type") or "").lower()
                cv = str(r.get("code_value") or "").strip()
                if not cv:
                    continue
                result.setdefault(ct, []).append(cv)

    return result


# =========================================================
# Reports / history
# =========================================================
# =========================================================
# Reports / history
# =========================================================
async def get_orders_stats() -> Dict[str, int]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT COUNT(*) c FROM orders")
            total = int((await c.fetchone())["c"])
            await c.execute("SELECT COUNT(*) c FROM orders WHERE status='CONFIRMED'")
            confirmed = int((await c.fetchone())["c"])
            await c.execute("SELECT COUNT(*) c FROM orders WHERE status='INIT'")
            init = int((await c.fetchone())["c"])
            return {"total": total, "confirmed": confirmed, "init": init, "other": total - confirmed - init}


async def get_recent_issues(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Получить последние выданные коды.
    В новой схеме связь через order_items.code_id, order_codes удалена.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT cd.code_value AS code,
                       u.tg_user_id AS user_id,
                       cd.issued_at,
                       o.id AS order_id,
                       u.first_name AS buyer_name,
                       u.phone AS buyer_phone,
                       u.tg_username
                  FROM order_items oi
                  JOIN codes cd ON cd.id = oi.code_id
                  JOIN orders o ON o.id = oi.order_id
                  LEFT JOIN users u ON u.id = o.user_id
                 WHERE oi.code_id IS NOT NULL
              ORDER BY cd.issued_at DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return await c.fetchall()



async def get_user_codes(
    user_id: int = 0,
    limit: int = 50,
    offset: int = 0,
    *,
    internal_user_id: int | None = None,
    max_user_id: int | str | None = None,
) -> List[Dict[str, Any]]:
    """
    Коды пользователя напрямую из таблицы codes.
    user_id - это telegram user_id.

    В новой схеме item_title в order_items отсутствует, поэтому название формируем из code_type/vendor_name/months_number.
    """
    internal_user_id = await _resolve_order_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=user_id or None,
        max_user_id=max_user_id,
    )
    if not internal_user_id:
        return []

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  id         AS code_id,
                  code_value AS code,
                  issued_at  AS issued_at,
                  months_number AS months_number,
                  expires_at AS expires_at,
                  code_type  AS code_type,
                  COALESCE(vendor_name, 'internal') AS source,
                  COALESCE(sell_price, 0) AS amount
                FROM codes
                WHERE user_id=%s
                  AND is_used=1
                  AND issued_at IS NOT NULL
                  AND (expires_at IS NULL OR expires_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 3 MONTH))
                ORDER BY issued_at DESC
                LIMIT %s OFFSET %s
                """,
                (internal_user_id, int(limit), int(offset)),
            )
            rows = await c.fetchall() or []

            for r in rows:
                ct = (r.get("code_type") or "").lower()
                mn = r.get("months_number")
                src = r.get("source")
                # item_title (для UI)
                if ct == "ofd_first":
                    title = "Первый ОФД"
                elif ct == "ofd_platform":
                    title = "Платформа ОФД"
                elif ct == "ofd_other":
                    title = f"ОФД ({src})" if src and src != "internal" else "ОФД "
                elif ct == "service" or ct == "service_order":
                    title = f"Лицензия/прошивка ({src})" if src and src != "internal" else "Лицензия/прошивка"
                elif ct == "edo":
                    title = f"ЭДО ({src})" if src and src != "internal" else "ЭДО"
                else:
                    title = "Код"

                if mn and ct in ("ofd_first", "ofd_platform", "ofd_other"):
                    try:
                        title = f"{title} ({int(mn)} мес.)"
                    except Exception:
                        pass

                r["item_title"] = title
                # legacy item_type для обратной совместимости
                r["item_type"] = ct or "ofd_first"

            return rows


async def get_user_purchases(
    user_id: int = 0,
    limit: int = 50,
    offset: int = 0,
    *,
    internal_user_id: int | None = None,
    max_user_id: int | str | None = None,
) -> List[Dict[str, Any]]:
    """
    "Мои покупки": поддерживает несколько кодов в одном заказе.
    В новой схеме связь через order_items.code_id.
    user_id - это telegram user_id.
    """
    internal_user_id = await _resolve_order_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=user_id or None,
        max_user_id=max_user_id,
    )
    if not internal_user_id:
        return []

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Важно: "Мои покупки" должны показывать детали по внешним ключам:
            # - order_items.code_id -> codes
            # - order_items.equipment_id -> equipment
            await c.execute(
                """
                SELECT
                  o.id AS order_id,
                  o.status,
                  o.amount AS order_amount,
                  o.paid_at,
                  oi.id AS order_item_id,
                  oi.qty,
                  oi.price,
                  cd.id AS code_id,
                  cd.code_type,
                  cd.code_value AS code,
                  cd.issued_at,
                  cd.expires_at,
                  cd.months_number,
                  cd.vendor_name,
                  eq.id AS equipment_id,
                  eq.name AS equipment_name,
                  eq.category AS equipment_category,
                  eq.is_used AS equipment_is_used
                FROM orders o
                JOIN order_items oi ON oi.order_id = o.id
                LEFT JOIN codes cd ON cd.id = oi.code_id
                LEFT JOIN equipment eq ON eq.id = oi.equipment_id
                WHERE o.user_id=%s
                  AND o.status='CONFIRMED'
                ORDER BY o.paid_at DESC, oi.id ASC
                LIMIT %s OFFSET %s
                """,
                (internal_user_id, int(limit), int(offset)),
            )
            rows = await c.fetchall() or []

            # Приводим к единому формату для UI (bot_registration.show_my_codes).
            # Никакой совместимости со старой схемой: типы строго как в БД.
            #   - коды: ofd_first, ofd_platform, ofd_other, service, edo
            #   - оборудование: equip
            out: list[dict] = []
            for r in rows:
                qty = int(r.get("qty") or 1)
                price = int(r.get("price") or 0)

                code_id = r.get("code_id")
                equipment_id = r.get("equipment_id")

                ct = (r.get("code_type") or "").strip().lower()
                vendor = (r.get("vendor_name") or "").strip()
                months_number = r.get("months_number")

                equipment_name = (r.get("equipment_name") or "").strip()

                if code_id:
                    item_type = ct or "code"
                    # Человеческое имя позиции
                    if ct == "ofd_first":
                        item_title = "Первый ОФД"
                    elif ct == "ofd_platform":
                        item_title = "Платформа ОФД"
                    elif ct == "ofd_other":
                        item_title = f"ОФД (внешний: {vendor})" if vendor else "ОФД (внешний)"
                    elif ct == "service" or ct == "service_order":
                        item_title = f"Лицензия/прошивка ({vendor})" if vendor else "Лицензия/прошивка"
                    elif ct == "edo":
                        item_title = f"ЭДО ({vendor})" if vendor else "ЭДО"
                    else:
                        item_title = "Код"

                    if months_number and ct in ("ofd_first", "ofd_platform", "ofd_other"):
                        try:
                            item_title = f"{item_title} ({int(months_number)} мес.)"
                        except Exception:
                            # months_number может быть NULL/строкой — это не критично
                            pass

                elif equipment_id:
                    item_type = "equip"
                    item_title = equipment_name or "Оборудование"
                else:
                    # Строка без FK (не должна возникать в нормальной работе)
                    item_type = "other"
                    item_title = "Покупка"

                out.append(
                    {
                        "order_id": r.get("order_id"),
                        "amount": price * qty,  # сумма позиции
                        "paid_at": r.get("paid_at"),
                        "issued_at": r.get("issued_at") or r.get("paid_at"),
                        "expires_at": r.get("expires_at"),
                        "code": r.get("code") or "",
                        "item_type": item_type,
                        "item_title": item_title,
                        "order_item_id": r.get("order_item_id"),
                        "qty": qty,
                        "price": price,
                        "equipment_id": equipment_id,
                        "code_id": code_id,
                    }
                )

            return out


async def get_user_codes_by_internal_id(internal_user_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Коды пользователя по внутреннему users.id (для MAX-бота)."""
    if not internal_user_id:
        return []

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  id         AS code_id,
                  code_value AS code,
                  issued_at  AS issued_at,
                  months_number AS months_number,
                  expires_at AS expires_at,
                  code_type  AS code_type,
                  COALESCE(vendor_name, 'internal') AS source,
                  COALESCE(sell_price, 0) AS amount
                FROM codes
                WHERE user_id=%s
                  AND is_used=1
                  AND issued_at IS NOT NULL
                  AND (expires_at IS NULL OR expires_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 3 MONTH))
                ORDER BY issued_at DESC
                LIMIT %s OFFSET %s
                """,
                (int(internal_user_id), int(limit), int(offset)),
            )
            rows = await c.fetchall() or []

            for r in rows:
                ct = (r.get("code_type") or "").lower()
                mn = r.get("months_number")
                src = r.get("source")
                if ct == "ofd_first":
                    title = "Первый ОФД"
                elif ct == "ofd_platform":
                    title = "Платформа ОФД"
                elif ct == "ofd_other":
                    title = f"ОФД ({src})" if src and src != "internal" else "ОФД "
                elif ct == "service" or ct == "service_order":
                    title = f"Лицензия/прошивка ({src})" if src and src != "internal" else "Лицензия/прошивка"
                elif ct == "edo":
                    title = f"ЭДО ({src})" if src and src != "internal" else "ЭДО"
                else:
                    title = "Код"

                if mn and ct in ("ofd_first", "ofd_platform", "ofd_other"):
                    try:
                        title = f"{title} ({int(mn)} мес.)"
                    except Exception:
                        pass

                r["item_title"] = title
                r["item_type"] = ct or "ofd_first"

            return rows


async def get_user_purchases_by_internal_id(internal_user_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Покупки пользователя по внутреннему users.id (для MAX-бота)."""
    if not internal_user_id:
        return []

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  o.id AS order_id,
                  o.status,
                  o.amount AS order_amount,
                  o.paid_at,
                  oi.id AS order_item_id,
                  oi.qty,
                  oi.price,
                  cd.id AS code_id,
                  cd.code_type,
                  cd.code_value AS code,
                  cd.issued_at,
                  cd.expires_at,
                  cd.months_number,
                  cd.vendor_name,
                  eq.id AS equipment_id,
                  eq.name AS equipment_name,
                  eq.category AS equipment_category,
                  eq.is_used AS equipment_is_used
                FROM orders o
                JOIN order_items oi ON oi.order_id = o.id
                LEFT JOIN codes cd ON cd.id = oi.code_id
                LEFT JOIN equipment eq ON eq.id = oi.equipment_id
                WHERE o.user_id=%s
                  AND o.status='CONFIRMED'
                ORDER BY o.paid_at DESC, oi.id ASC
                LIMIT %s OFFSET %s
                """,
                (int(internal_user_id), int(limit), int(offset)),
            )
            rows = await c.fetchall() or []

            out: list[dict] = []
            for r in rows:
                qty = int(r.get("qty") or 1)
                price = int(r.get("price") or 0)
                code_id = r.get("code_id")
                equipment_id = r.get("equipment_id")
                ct = (r.get("code_type") or "").strip().lower()
                vendor = (r.get("vendor_name") or "").strip()
                months_number = r.get("months_number")
                equipment_name = (r.get("equipment_name") or "").strip()

                if code_id:
                    item_type = ct or "code"
                    if ct == "ofd_first":
                        item_title = "Первый ОФД"
                    elif ct == "ofd_platform":
                        item_title = "Платформа ОФД"
                    elif ct == "ofd_other":
                        item_title = f"ОФД (внешний: {vendor})" if vendor else "ОФД (внешний)"
                    elif ct == "service" or ct == "service_order":
                        item_title = f"Лицензия/прошивка ({vendor})" if vendor else "Лицензия/прошивка"
                    elif ct == "edo":
                        item_title = f"ЭДО ({vendor})" if vendor else "ЭДО"
                    else:
                        item_title = "Код"
                    if months_number and ct in ("ofd_first", "ofd_platform", "ofd_other"):
                        try:
                            item_title = f"{item_title} ({int(months_number)} мес.)"
                        except Exception:
                            pass
                elif equipment_id:
                    item_type = "equip"
                    item_title = equipment_name or "Оборудование"
                else:
                    item_type = "other"
                    item_title = "Покупка"

                out.append(
                    {
                        "order_id": r.get("order_id"),
                        "amount": price * qty,
                        "paid_at": r.get("paid_at"),
                        "issued_at": r.get("issued_at") or r.get("paid_at"),
                        "expires_at": r.get("expires_at"),
                        "code": r.get("code") or "",
                        "item_type": item_type,
                        "item_title": item_title,
                        "order_item_id": r.get("order_item_id"),
                        "qty": qty,
                        "price": price,
                        "equipment_id": equipment_id,
                        "code_id": code_id,
                    }
                )

            return out


async def list_codes_expiring_by_expires_at(days_notice: List[int] | None = None) -> List[Dict[str, Any]]:
    '''Коды (обычно ОФД) с заполненным expires_at, у которых до окончания осталось N дней.
    Используется для ежедневных напоминаний по купленным кодам (в т.ч. внешним).
    В новой схеме user_id - это int (внутренний id), возвращаем tg_user_id.'''
    if not days_notice:
        days_notice = [30, 7, 1, 0]

    days_notice = [int(x) for x in days_notice]
    placeholders = ",".join(["%s"] * len(days_notice))

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f'''
                SELECT
                  cd.id AS code_id,
                  u.tg_user_id AS user_id,
                  cd.code_value AS code,
                  cd.code_type,
                  cd.vendor_name AS source,
                  cd.vendor_name,
                  cd.months_number,
                  cd.expires_at,
                  DATEDIFF(DATE(cd.expires_at), CURDATE()) AS days_left
                FROM codes cd
                JOIN users u ON u.id = cd.user_id
                WHERE cd.user_id IS NOT NULL
                  AND cd.expires_at IS NOT NULL
                  AND cd.is_used=1
                  AND cd.code_type IN ('ofd_first','ofd_platform','ofd_other','service','edo')
                  AND DATEDIFF(DATE(cd.expires_at), CURDATE()) IN ({placeholders})
                ORDER BY cd.expires_at
                ''',
                tuple(days_notice),
            )
            return await c.fetchall()

async def list_codes_expiring_by_expires_at_max(days_notice: List[int] | None = None) -> List[Dict[str, Any]]:
    """Аналог list_codes_expiring_by_expires_at, но возвращает max_user_id."""
    if not days_notice:
        days_notice = [30, 7, 1, 0]

    days_notice = [int(x) for x in days_notice]
    placeholders = ",".join(["%s"] * len(days_notice))

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f'''
                SELECT
                  cd.id AS code_id,
                  u.max_user_id AS user_id,
                  cd.code_value AS code,
                  cd.code_type,
                  cd.vendor_name AS source,
                  cd.vendor_name,
                  cd.months_number,
                  cd.expires_at,
                  DATEDIFF(DATE(cd.expires_at), CURDATE()) AS days_left
                FROM codes cd
                JOIN users u ON u.id = cd.user_id
                WHERE cd.user_id IS NOT NULL
                  AND cd.expires_at IS NOT NULL
                  AND cd.is_used=1
                  AND cd.code_type IN ('ofd_first','ofd_platform','ofd_other','service','edo')
                  AND DATEDIFF(DATE(cd.expires_at), CURDATE()) IN ({placeholders})
                  AND u.max_user_id IS NOT NULL
                ORDER BY cd.expires_at
                ''',
                tuple(days_notice),
            )
            return await c.fetchall()


async def purge_expired_codes_older_than_months(months: int = 3) -> int:
    """Удаляет из БД использованные коды, истёкшие более чем N месяцев назад.

    Это нужно, чтобы такие коды не показывались в профиле/покупках.
    В новой схеме order_codes удалена, связь через order_items.code_id.
    Возвращает количество удалённых записей из `codes` (приблизительно).
    """
    try:
        months_i = max(1, int(months))
    except Exception:
        months_i = 3

    cutoff = datetime.utcnow() - timedelta(days=31 * months_i)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            # 1) Сначала убираем связи из order_items (ставим NULL)
            await c.execute(
                """
                UPDATE order_items oi
                  JOIN codes cd ON cd.id = oi.code_id
                   SET oi.code_id = NULL
                 WHERE cd.is_used=1
                   AND cd.expires_at IS NOT NULL
                   AND cd.expires_at < %s
                """,
                (cutoff,),
            )

            # 2) Потом удаляем сами коды
            await c.execute(
                """
                DELETE FROM codes
                 WHERE is_used=1
                   AND expires_at IS NOT NULL
                   AND expires_at < %s
                """,
                (cutoff,),
            )
            deleted = int(getattr(c, "rowcount", 0) or 0)

            await _maybe_await(conn.commit())
            return deleted


async def get_order_item_ids_with_codes(order_id: str) -> set[int]:
    """
    Возвращает id строк заказа, у которых уже есть привязанный код.
    В новой схеме связь через order_items.code_id.
    """
    try:
        oid = int(order_id)
    except (ValueError, TypeError):
        return set()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT id FROM order_items WHERE order_id=%s AND code_id IS NOT NULL",
                (oid,),
            )
            rows = await c.fetchall() or []
            out: set[int] = set()
            for r in rows:
                try:
                    out.add(int(r["id"]))
                except Exception:
                    pass
            return out



async def get_order_reserved_external_codes(order_id: str) -> list[dict]:
    """Строки заказа с внешними кодами, которые были забронированы ДО оплаты и ещё не заполнены.
    Возвращает список dict с полями: order_item_id, code_id, code_type, operator_id, type_id, vseofd_order_id, sell_price.
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
                  cd.id AS code_id,
                  cd.code_type,
                  cd.operator_id,
                  cd.type_id,
                  cd.vseofd_order_id,
                  cd.sell_price,
                  oi.price
                FROM order_items oi
                JOIN codes cd ON cd.id = oi.code_id
                WHERE oi.order_id=%s
                  AND oi.code_id IS NOT NULL
                  AND cd.code_type IN ('ofd_other','service','edo','service_order')
                  AND cd.issued_at IS NULL
                ORDER BY oi.id
                """,
                (oid,),
            )
            return await c.fetchall() or []


async def save_external_code_for_order_item(
    *,
    order_id: str,
    order_item_id: int,
    user_id: str,
    code_value: str,
    source_price: float | None,
    sell_price: float | None,
    vendor_name: str | None,
    months_number: int | None,
    code_type: str,
) -> int:
    """Записывает значение внешнего кода в уже забронированную строку `codes`
    и привязывает (если ещё не привязан) к order_items.code_id.

    Предполагается новая логика:
    - при оформлении заказа ДО оплаты мы создаём заглушку в `codes` (code_value=''),
      и в order_items.code_id кладём её id;
    - после оплаты сюда пишем реальный code_value и issued_at/expires_at.

    Идемпотентно: если у связанного кода уже есть code_value и issued_at — просто возвращаем code_id.
    """
    try:
        oid = int(order_id)
        oiid = int(order_item_id)
    except (ValueError, TypeError):
        raise ValueError("Invalid order_id/order_item_id")

    # типы
    type_map = {
        "ofd": "ofd_other",
        "ofd_other": "ofd_other",
        "service": "service",
        "edo": "edo",
    }
    ct = type_map.get((code_type or "ofd_other").strip().lower(), "ofd_other")

    # months_number -> expires_at вычисляем в SQL (безопаснее)
    mn = None
    try:
        mn = int(months_number) if months_number is not None else None
    except Exception:
        mn = None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            await c.execute(
                """
                SELECT oi.code_id, cd.code_value, cd.issued_at
                  FROM order_items oi
                  LEFT JOIN codes cd ON cd.id = oi.code_id
                 WHERE oi.id=%s AND oi.order_id=%s
                 LIMIT 1
                """,
                (oiid, oid),
            )
            row = await c.fetchone() or {}
            code_id = row.get("code_id")
            if code_id and (row.get("issued_at") is not None) and str(row.get("code_value") or "").strip():
                await _maybe_await(conn.commit())
                return int(code_id)

            if not code_id:
                raise RuntimeError("order_item has no reserved code_id")

            cv = str(code_value or "").strip()
            if not cv:
                raise ValueError("Empty code_value")

            await c.execute(
                """
                UPDATE codes
                   SET code_value=%s,
                       code_type=%s,
                       issued_at=COALESCE(issued_at, UTC_TIMESTAMP()),
                       is_used=1,
                       source_price=COALESCE(%s, source_price),
                       sell_price=COALESCE(%s, sell_price),
                       vendor_name=COALESCE(%s, vendor_name),
                       months_number=COALESCE(%s, months_number),
                       expires_at=CASE
                           WHEN COALESCE(%s, months_number) IS NULL THEN expires_at
                           WHEN expires_at IS NOT NULL THEN expires_at
                           ELSE DATE_ADD(UTC_TIMESTAMP(), INTERVAL COALESCE(%s, months_number) MONTH)
                       END
                 WHERE id=%s
                """,
                (cv, ct, source_price, sell_price, vendor_name, mn, mn, mn, int(code_id)),
            )

            await _maybe_await(conn.commit())
            return int(code_id)


async def get_order_codes_detailed(order_id: str) -> list[dict]:
    """
    Получить детальную информацию о кодах заказа.
    В новой схеме связь через order_items.code_id.
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
                  cd.code_type         AS code_type,
                  cd.code_value        AS code,
                  cd.vendor_name       AS vendor_name,
                  COALESCE(cd.vendor_name, 'internal') AS source,
                  cd.expires_at,
                  cd.months_number
                FROM order_items oi
                JOIN codes cd ON cd.id = oi.code_id
                WHERE oi.order_id=%s AND oi.code_id IS NOT NULL AND cd.code_type != "service_order"
                ORDER BY oi.id
                """,
                (oid,),
            )
            return await c.fetchall() or []




async def update_order_item_type(order_item_id: int, item_type: str) -> None:
    """Легаси: в актуальной схеме order_items больше не хранит item_type/legacy_item_type.
    Оставлено как no-op для совместимости вызовов."""
    return
