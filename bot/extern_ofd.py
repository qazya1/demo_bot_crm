import json
from collections import defaultdict
from pathlib import Path
import os

import mysql.connector

import bot.ofd_api as ofd_api
from db.connection import MYSQL_CONFIG


def _load_json(filename: str) -> dict:
    base = Path(__file__).resolve().parent
    path = base / filename
    if not path.exists():
        path = Path(filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


LEGACY_OPERATORS = _load_json("operators.json")
LEGACY_OFD_DEADLINES = _load_json("ofd_deadlines.json")

_HAVE_OWN_OFD = {"4", "8"}
BALANCE_TRESHOLD = float(os.getenv("BALANCE_TRESHOLD", -29000))


def _mysql_sync_config() -> dict:
    return {
        "host": MYSQL_CONFIG.get("host"),
        "port": MYSQL_CONFIG.get("port"),
        "user": MYSQL_CONFIG.get("user"),
        "password": MYSQL_CONFIG.get("password"),
        "database": MYSQL_CONFIG.get("database"),
        "autocommit": True,
        "use_pure": MYSQL_CONFIG.get("use_pure", True),
        "ssl_disabled": MYSQL_CONFIG.get("ssl_disabled", True),
    }


def _load_external_catalog_reference() -> tuple[dict[str, dict[str, str]], dict[str, dict]]:
    operators_by_category: dict[str, dict[str, str]] = {
        "ofd": {},
        "service": {},
        "services_for_orders": {},
        "edo": {},
    }
    type_map: dict[str, dict] = {}

    try:
        conn = mysql.connector.connect(**_mysql_sync_config())
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT category, operator_id, name
            FROM ext_product_operators
            WHERE is_active = 1
            ORDER BY sort_order ASC, name ASC
            """
        )
        for row in cur.fetchall() or []:
            cat = str(row.get("category") or "").strip().lower()
            op = str(row.get("operator_id") or "").strip()
            name = str(row.get("name") or "").strip()
            if cat and op and name:
                operators_by_category.setdefault(cat, {})[op] = name

        cur.execute(
            """
            SELECT type_id, name, months_number
            FROM ext_product_types
            WHERE is_active = 1
            ORDER BY sort_order ASC, type_id ASC
            """
        )
        for row in cur.fetchall() or []:
            type_id = str(row.get("type_id") or "").strip()
            if not type_id:
                continue
            type_map[type_id] = {
                "name": str(row.get("name") or type_id).strip(),
                "months_number": row.get("months_number"),
            }
        cur.close()
        conn.close()
    except Exception:
        operators_by_category = {
            "ofd": dict(LEGACY_OPERATORS.get("ofd_operators") or {}),
            "service": dict(LEGACY_OPERATORS.get("services") or {}),
            "services_for_orders": dict(LEGACY_OPERATORS.get("services_for_orders") or {}),
            "edo": dict(LEGACY_OPERATORS.get("edo") or {}),
        }
        type_map = dict(LEGACY_OFD_DEADLINES)

    return operators_by_category, type_map


def _sync_ext_products(category: str, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        conn = mysql.connector.connect(**_mysql_sync_config())
        cur = conn.cursor()
        for row in rows:
            cur.execute(
                """
                INSERT INTO ext_products (
                    category, operator_id, type_id, external_name,
                    source_price, is_active, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, 1, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE
                    external_name = VALUES(external_name),
                    source_price = VALUES(source_price),
                    last_seen_at = UTC_TIMESTAMP()
                """,
                (
                    row["category"],
                    row["operator_id"],
                    row["type_id"],
                    row.get("external_name"),
                    row.get("source_price"),
                ),
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass


def group_by_operator_then_type_sorted(products, sort_by_name: bool = True) -> dict:
    grouped = defaultdict(lambda: defaultdict(list))

    for product in products or []:
        operator_id = product.get("operatorId")
        type_id = product.get("typeId")
        grouped[operator_id][type_id].append(product)

    result = {}
    for operator_id in sorted(grouped.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
        result[operator_id] = {}
        for type_id in sorted(grouped[operator_id].keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
            if sort_by_name:
                result[operator_id][type_id] = sorted(
                    grouped[operator_id][type_id],
                    key=lambda x: str(x.get("name") or "")
                )
            else:
                result[operator_id][type_id] = grouped[operator_id][type_id]
    return result


def _category_map(category: str, operators_by_category: dict[str, dict[str, str]]) -> dict:
    c = (category or "ofd").strip().lower()
    if c == "services_for_orders":
        return operators_by_category.get("services_for_orders") or {}
    if c in ("service", "services"):
        return operators_by_category.get("service") or {}
    if c == "edo":
        return operators_by_category.get("edo") or {}
    return operators_by_category.get("ofd") or {}


def _build_tree(category: str) -> dict:
    operators_by_category, type_map = _load_external_catalog_reference()
    mapping = _category_map(category, operators_by_category)
    items_tree = group_by_operator_then_type_sorted(ofd_api.get_price_list())

    out: dict = {}
    seen_rows: list[dict] = []

    for op_id, by_type in (items_tree or {}).items():
        op_id_s = str(op_id)
        if op_id_s not in mapping:
            continue

        if (category or "ofd").lower() == "ofd" and op_id_s in _HAVE_OWN_OFD:
            continue

        op_name = str(mapping.get(op_id_s) or op_id_s).strip()
        if not op_name:
            continue

        out.setdefault(op_name, {})

        for type_id, prod_list in (by_type or {}).items():
            type_id_s = str(type_id)
            if type_id_s not in type_map:
                continue

            prod = None
            for p in (prod_list or []):
                try:
                    if int(p.get("count") or 0) > 0 or type_id_s == "9":
                        prod = p
                        break
                except Exception:
                    continue
            if not prod:
                continue

            term = str(type_map[type_id_s].get("name") or type_id_s)
            mn = type_map[type_id_s].get("months_number")

            item = dict(prod)
            item["months_number"] = mn
            out[op_name][term] = item
            seen_rows.append({
                "category": (category or "ofd").strip().lower(),
                "operator_id": int(op_id_s),
                "type_id": int(type_id_s),
                "external_name": str(prod.get("name") or "").strip() or None,
                "source_price": prod.get("price"),
            })

        if not out[op_name]:
            out.pop(op_name, None)

    _sync_ext_products((category or "ofd").strip().lower(), seen_rows)
    out = dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
    return out


def get_products(category: str = "ofd"):
    try:
        profile_info = ofd_api.get_profile_info()
        if (profile_info or {}).get("balance", 0) <= BALANCE_TRESHOLD:
            return []
    except Exception as e:
        # Логируем ошибку но не прерываем работу - возвращаем пустой список
        # для локального тестирования без настроенного ОФД
        import logging
        logging.getLogger("extern_ofd").warning(f"OFD API недоступна: {e}. Возвращаем пустой каталог.")
        return []
    return _build_tree(category)


def get_products_ofd():
    return get_products("ofd")


def reserve_ofd_order(operator_id: str, type_id: str, count: int = 1):
    return ofd_api.reserve_codes(operator_id, type_id, count)


def pay_reserved_ofd_order(order_id: str):
    return ofd_api.payment(order_id)


def create_ofd_order(*, operator_id: str, type_id: str, count: int = 1):
    reserve_ans = reserve_ofd_order(operator_id, type_id, count)
    oid = str((reserve_ans or {}).get("orderId") or "").strip()
    if not oid:
        return []
    pay_ans = pay_reserved_ofd_order(oid)
    return (pay_ans or {}).get("codes") or []
