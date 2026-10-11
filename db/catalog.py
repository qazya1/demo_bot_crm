from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from db.connection import get_conn, _cur, _maybe_await


# --- KKT cashboxes (кассы из каталога) ---

async def get_kkt_cashboxes():
    """
    Возвращает список касс:
    [{id, name, image_name, price}, ...]
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, name, image_name, price FROM kkt_cashboxes ORDER BY id
                """
            )
            return await c.fetchall()


async def get_kkt_cashbox_by_id(kkt_id: int):
    """
    Возвращает одну кассу или None.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, name, image_name, price FROM kkt_cashboxes WHERE id = %s
                """,
                (int(kkt_id),),
            )
            return await c.fetchone()



# --- Equipment catalog (таблица equipment) ---

async def get_equipment_categories(is_used: int = 0, include_fn: bool = False) -> List[str]:
    """Список категорий оборудования для меню.
    В новой схеме is_active и sort_order удалены, category - ENUM.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT DISTINCT category
                  FROM equipment
                 WHERE is_used=%s AND (LOWER(category) <> 'фн' OR %s)
                 ORDER BY category ASC
                """,
                (int(is_used),int(include_fn)),
            )
            rows = await c.fetchall() or []
            out: List[str] = []
            for r in rows:
                cat = (r.get("category") if isinstance(r, dict) else None) or ""
                cat = str(cat).strip()
                if cat:
                    out.append(cat)
            return out


async def get_equipment_items(category: str, is_used: int = 0) -> List[Dict[str, Any]]:
    """Список товаров в категории.
    В новой схеме is_active, sort_order, description удалены.
    Требование: если count < 1 — товар не показываем. Если count IS NULL — ограничений нет.
    """
    cat = (category or "").strip()
    if not cat:
        return []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, name, category, is_used, price, image_name, `count`
                  FROM equipment
                 WHERE is_used=%s
                   AND category=%s
                   AND (`count` IS NULL OR `count` > 0)
                 ORDER BY id ASC
                """,
                (int(is_used), cat),
            )
            return await c.fetchall() or []


async def get_equipment_by_id(equipment_id: int) -> Optional[Dict[str, Any]]:
    """Одна позиция оборудования или None."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT * FROM equipment WHERE id=%s LIMIT 1",
                (int(equipment_id),),
            )
            return await c.fetchone()


async def get_equipment_id_by_name_category(name: str, category: str) -> Optional[int]:
    """Ищет id оборудования по точному (name, category).
    Используется, чтобы привязать товары из отдельного интерфейса (например ФН) к equipment.id.
    В новой схеме is_active удалён.
    Возвращает None если не найдено.
    """
    nm = (name or "").strip()
    cat = (category or "").strip()
    if not nm or not cat:
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id
                  FROM equipment
                 WHERE LOWER(category)=LOWER(%s)
                   AND name=%s
                 LIMIT 1
                """,
                (cat, nm),
            )
            row = await c.fetchone()
            if not row:
                return None
            try:
                return int(row.get("id") if isinstance(row, dict) else row[0])
            except Exception:
                return None


async def get_equipment_available_count(equipment_id: int) -> Optional[int]:
    """Возвращает доступное количество:
    - None, если `count` IS NULL (без ограничений)
    - int >= 0, если значение задано
    - 0, если товар не найден
    В новой схеме is_active удалён из equipment.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT `count` FROM equipment WHERE id=%s LIMIT 1",
                (int(equipment_id),),
            )
            row = await c.fetchone()
            if not row:
                return 0
            cnt = row.get("count") if isinstance(row, dict) else None
            if cnt is None:
                return None
            try:
                return int(cnt)
            except Exception:
                return 0




def _load_external_catalog_seed() -> tuple[dict, dict]:
    base = Path(__file__).resolve().parent.parent / 'bot'
    with open(base / 'operators.json', 'r', encoding='utf-8') as f:
        operators = json.load(f)
    with open(base / 'ofd_deadlines.json', 'r', encoding='utf-8') as f:
        deadlines = json.load(f)
    return operators, deadlines


async def ensure_ext_products_schema() -> None:
    operators, deadlines = _load_external_catalog_seed()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS ext_product_operators (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    category ENUM('ofd', 'service', 'services_for_orders', 'edo') NOT NULL,
                    operator_id INT NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    sort_order INT NOT NULL DEFAULT 0,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_ext_product_operator (category, operator_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS ext_product_types (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    type_id INT NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    months_number INT DEFAULT NULL,
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    sort_order INT NOT NULL DEFAULT 0,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_ext_product_type (type_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS ext_products (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    category ENUM('ofd', 'service', 'services_for_orders', 'edo') NOT NULL,
                    operator_id INT NOT NULL,
                    type_id INT NOT NULL,
                    external_name VARCHAR(255) DEFAULT NULL,
                    source_price INT DEFAULT NULL,
                    price_fixed INT DEFAULT NULL,
                    markup_pct DECIMAL(8,2) DEFAULT NULL,
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    comment VARCHAR(255) DEFAULT NULL,
                    last_seen_at DATETIME DEFAULT NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_ext_product (category, operator_id, type_id),
                    KEY idx_ext_products_active (category, is_active)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            category_map = {
                'ofd_operators': 'ofd',
                'services': 'service',
                'services_for_orders': 'services_for_orders',
                'edo': 'edo',
            }
            for source_key, category in category_map.items():
                for sort_order, (operator_id, name) in enumerate((operators.get(source_key) or {}).items(), start=1):
                    await c.execute(
                        """
                        INSERT INTO ext_product_operators (category, operator_id, name, sort_order)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            name = VALUES(name),
                            sort_order = VALUES(sort_order)
                        """,
                        (category, int(operator_id), str(name).strip(), sort_order),
                    )

            for sort_order, (type_id, info) in enumerate((deadlines or {}).items(), start=1):
                await c.execute(
                    """
                    INSERT INTO ext_product_types (type_id, name, months_number, sort_order)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        months_number = VALUES(months_number),
                        sort_order = VALUES(sort_order)
                    """,
                    (int(type_id), str((info or {}).get('name') or type_id).strip(), (info or {}).get('months_number'), sort_order),
                )

            try:
                await c.execute(
                    """
                    INSERT INTO ext_products (category, operator_id, type_id, price_fixed, markup_pct, is_active, comment, updated_at)
                    SELECT e.category, e.operator_id, e.type_id, e.price_fixed, e.markup_pct, e.is_active, e.comment, e.updated_at
                    FROM ext_price_overrides e
                    LEFT JOIN ext_products p
                      ON p.category = e.category AND p.operator_id = e.operator_id AND p.type_id = e.type_id
                    WHERE p.id IS NULL
                    """
                )
            except Exception:
                pass
        await conn.commit()

# --- External products pricing (таблица ext_products) ---

async def get_ext_product(category: str, operator_id: int, type_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает активную настройку товара по (category, operator_id, type_id) или None."""
    cat = (category or "").strip().lower()
    if cat not in {"ofd", "service", "services_for_orders", "edo"}:
        return None
    await ensure_ext_products_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT category, operator_id, type_id, external_name, source_price,
                       price_fixed, markup_pct, is_active, comment, last_seen_at
                  FROM ext_products
                 WHERE category=%s AND operator_id=%s AND type_id=%s AND is_active=1
                 LIMIT 1
                """,
                (cat, int(operator_id), int(type_id)),
            )
            return await c.fetchone()


async def get_ext_products_all(active_only: bool = True) -> List[Dict[str, Any]]:
    """Все настройки внешних товаров (удобно кэшировать в боте)."""
    await ensure_ext_products_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if active_only:
                await c.execute(
                    """
                    SELECT category, operator_id, type_id, external_name, source_price,
                           price_fixed, markup_pct, is_active, comment, last_seen_at
                      FROM ext_products
                     WHERE is_active=1
                    """
                )
            else:
                await c.execute(
                    """
                    SELECT category, operator_id, type_id, external_name, source_price,
                           price_fixed, markup_pct, is_active, comment, last_seen_at
                      FROM ext_products
                    """
                )
            return await c.fetchall() or []


# Backward compatibility wrappers
async def get_ext_price_override(category: str, operator_id: int, type_id: int) -> Optional[Dict[str, Any]]:
    return await get_ext_product(category, operator_id, type_id)


async def get_ext_price_overrides_all(active_only: bool = True) -> List[Dict[str, Any]]:
    return await get_ext_products_all(active_only=active_only)
