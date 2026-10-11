"""
max_catalog.py — Каталог, корзина, оплата для MAX-бота.

Порт логики из bot_catalog.py с адаптацией под MAX API:
- Вместо Telegram InlineKeyboardButton -> max_api.InlineButton
- Вместо update.callback_query.answer() -> ctx.api.answer_callback()
- Корзина в ctx.user_data["cart"] (тот же формат)
- _send_or_edit переиспользуем из max_registration
"""

import os
import sys
import time
import math
import json
import logging
import asyncio
import concurrent.futures
from typing import Optional

from max_api import (
    MaxMessage, MaxCallback,
    InlineKeyboard, InlineButton,
)
from max_bot_main import MaxBotContext

# Переиспользуем утилиты из max_registration
from max_registration import _send_or_edit, _set_state, ST_IDLE, _resolve_chat_id, _max_ui

from pathlib import Path

_bot_dir = Path(__file__).resolve().parent
_project_dir = _bot_dir.parent
sys.path.insert(0, str(_project_dir))

import datetime as _dt

from db import (
    upsert_max_user,
    get_user_by_internal_id,
    is_user_verified_by_internal_id,

    create_order,
    get_order,
    set_order_payment_id,
    set_order_status,
    set_order_item_meta,
    confirm_order_paid,

    save_order_items,
    get_order_items,
    issue_code_for_order,

    get_free_codes_count,
    reserve_pool_code_for_user,
    reserve_external_code_for_user,
    rollback_order_reservations,
    list_orders_awaiting_payment,

    get_stats,
    get_orders_stats,
    get_recent_issues,
    add_codes_bulk,
    get_all_clients_with_ofd_max,
    get_order_reserved_external_codes,

    get_equipment_categories,
    get_equipment_items,
    get_equipment_by_id,
    get_equipment_id_by_name_category,
    get_equipment_available_count,

    get_ext_products_all,
    get_order_codes_detailed,
    save_external_code_for_order_item,
    list_directors,
    get_system_setting_number,
)

from extern_ofd import get_products, reserve_ofd_order, pay_reserved_ofd_order
from tinkoff import init_payment, get_payment_state, build_tinkoff_order_id
from django_crm import push_sale_to_crm, push_verification_to_crm

logger = logging.getLogger(__name__)

TESTER_MAX_IDS: set[int] = set()
for _x in os.getenv("TESTER_MAX_IDS", "").replace(" ", "").split(","):
    if _x:
        try:
            TESTER_MAX_IDS.add(int(_x))
        except ValueError:
            pass


def _is_tester_max(max_user_id: int | str | None) -> bool:
    try:
        return int(max_user_id) in TESTER_MAX_IDS
    except Exception:
        return False

# ── Поток для синхронных вызовов (extern_ofd, tinkoff) ───────────────
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


async def _run_in_thread(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


# ══════════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ: статический каталог + цены из ENV
# ══════════════════════════════════════════════════════════════════════

def _env_price(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


PAYMENT_WAIT_TIMEOUT_SEC = _env_price("PAYMENT_WAIT_TIMEOUT_SEC", 30 * 60)
PAYMENT_WAIT_LOOKBACK_MIN = max(1, (PAYMENT_WAIT_TIMEOUT_SEC + 59) // 60 + 5)


PRODUCTS = {
    "ofd_first":    {"name": "Первый ОФД (12 месяцев)",    "price": _env_price("PRICE_OFD_FIRST", 2500)},
    "ofd_platform": {"name": "Платформа ОФД (12 месяцев)", "price": _env_price("PRICE_OFD_PLATFORM", 2500)},
}


# ══════════════════════════════════════════════════════════════════════
#  КЭШИ внешних товаров (OFD / Service / EDO)
# ══════════════════════════════════════════════════════════════════════
# Кэши хранят ответ get_products() и время обновления.
# TTL = 2 часа — потом обновляем при следующем запросе.

EXT_OFD_CACHE     = {"products": None, "updated_at": 0}
EXT_SERVICE_CACHE  = {"products": None, "updated_at": 0}
EXT_SERVICE_ORDERS_CACHE = {"products": None, "updated_at": 0}
EXT_EDO_CACHE      = {"products": None, "updated_at": 0}
EXT_OFD_TTL = 60 * 60 * 2  # 2 часа


def refresh_ext_ofd_products(force: bool = False):
    now = time.time()
    if force or EXT_OFD_CACHE["products"] is None or now - EXT_OFD_CACHE["updated_at"] > EXT_OFD_TTL:
        products = get_products("ofd")
        if not isinstance(products, dict):
            products = {}
        EXT_OFD_CACHE["products"] = products
        EXT_OFD_CACHE["updated_at"] = now
    return EXT_OFD_CACHE["products"]


def refresh_ext_service_products(force: bool = False):
    now = time.time()
    if force or EXT_SERVICE_CACHE["products"] is None or now - EXT_SERVICE_CACHE["updated_at"] > EXT_OFD_TTL:
        products = get_products("service")
        if not isinstance(products, dict):
            products = {}
        EXT_SERVICE_CACHE["products"] = products
        EXT_SERVICE_CACHE["updated_at"] = now
    return EXT_SERVICE_CACHE["products"]


def refresh_ext_service_orders_products(force: bool = False):
    now = time.time()
    if force or EXT_SERVICE_ORDERS_CACHE["products"] is None or now - EXT_SERVICE_ORDERS_CACHE["updated_at"] > EXT_OFD_TTL:
        products = get_products("services_for_orders")
        if not isinstance(products, dict):
            products = {}
        EXT_SERVICE_ORDERS_CACHE["products"] = products
        EXT_SERVICE_ORDERS_CACHE["updated_at"] = now
    return EXT_SERVICE_ORDERS_CACHE["products"]


def refresh_ext_edo_products(force: bool = False):
    now = time.time()
    if force or EXT_EDO_CACHE["products"] is None or now - EXT_EDO_CACHE["updated_at"] > EXT_OFD_TTL:
        products = get_products("edo")
        if not isinstance(products, dict):
            products = {}
        EXT_EDO_CACHE["products"] = products
        EXT_EDO_CACHE["updated_at"] = now
    return EXT_EDO_CACHE["products"]


EXT_OFD_MARKUP_PCT = float(os.getenv("EXTERNAL_OFD_MARKUP_PCT", 740))
EXT_SERVICES_MARKUP_PCT = float(os.getenv("EXTERNAL_SERVICES_MARKUP_PCT", 740))
EXT_EDO_MARKUP_PCT = float(os.getenv("EXTERNAL_EDO_MARKUP_PCT", 740))

SYSTEM_SETTING_MARKUP_NAMES = {
    "ofd": "EXTERNAL_OFD_MARKUP_PCT",
    "service": "EXTERNAL_SERVICES_MARKUP_PCT",
    "services_for_orders": "EXTERNAL_SERVICES_MARKUP_PCT",
    "edo": "EXTERNAL_EDO_MARKUP_PCT",
}


async def _get_default_external_markup_pct(category: str) -> float:
    key = SYSTEM_SETTING_MARKUP_NAMES.get((category or "").strip().lower())
    fallback_map = {
        "EXTERNAL_OFD_MARKUP_PCT": EXT_OFD_MARKUP_PCT,
        "EXTERNAL_SERVICES_MARKUP_PCT": EXT_SERVICES_MARKUP_PCT,
        "EXTERNAL_EDO_MARKUP_PCT": EXT_EDO_MARKUP_PCT,
    }
    fallback = fallback_map.get(key, 200.0)
    if not key:
        return fallback
    value = await get_system_setting_number(key, default=fallback)
    return float(value if value is not None else fallback)


# ══════════════════════════════════════════════════════════════════════
#  НАЦЕНКА: ext_products из БД + fallback из ENV
# ══════════════════════════════════════════════════════════════════════
# Приоритет цены:
#   1) ext_products.price_fixed (фиксированная цена)
#   2) ext_products.markup_pct  (наценка в %)
#   3) fallback к system_settings / ENV, если override не задан

_EXT_PRICE_OVR_CACHE = {"data": None, "updated_at": 0.0}
_EXT_PRICE_OVR_TTL = 60 * 5  # 5 минут


async def _get_ext_products_pricing(force: bool = False) -> dict:
    now = time.time()
    if force or _EXT_PRICE_OVR_CACHE["data"] is None or (now - float(_EXT_PRICE_OVR_CACHE["updated_at"] or 0)) > _EXT_PRICE_OVR_TTL:
        rows = await get_ext_products_all(active_only=True)
        data = {}
        for r in rows or []:
            try:
                cat = str(r.get("category") or "").strip().lower()
                op = int(r.get("operator_id") or 0)
                tp = int(r.get("type_id") or 0)
                if not cat or not op or not tp:
                    continue
                data[(cat, op, tp)] = dict(r)
            except Exception:
                continue
        _EXT_PRICE_OVR_CACHE["data"] = data
        _EXT_PRICE_OVR_CACHE["updated_at"] = now
    return _EXT_PRICE_OVR_CACHE["data"] or {}


async def ext_sell_price(category: str, operator_id: int, type_id: int, source_price) -> int:
    """Цена продажи для внешнего товара с учётом наценки."""
    try:
        src = float(source_price or 0)
    except Exception:
        src = 0.0

    cat = (category or "").strip().lower()
    try:
        op = int(operator_id)
        tp = int(type_id)
    except Exception:
        op, tp = 0, 0

    ov = None
    if cat in {"ofd", "service", "edo", "services_for_orders"} and op and tp:
        ov = (await _get_ext_products_pricing()).get((cat, op, tp))

    # 1) фиксированная цена
    if ov and ov.get("price_fixed") is not None:
        try:
            return int(ov["price_fixed"])
        except Exception:
            pass

    # 2) наценка из overrides или fallback из system_settings / ENV
    pct = None
    if ov is not None:
        pct = ov.get("markup_pct")
    if pct is None:
        pct = await _get_default_external_markup_pct(cat)

    try:
        pct_f = float(pct)
    except Exception:
        pct_f = 200

    return int(math.ceil(src * (1.0 + pct_f / 100.0)))


# ══════════════════════════════════════════════════════════════════════
#  УТИЛИТА: проверка верификации перед покупкой
# ══════════════════════════════════════════════════════════════════════

async def _require_verified(ctx: MaxBotContext, chat_id: str, internal_id: int,
                            cb: Optional[MaxCallback] = None) -> bool:
    """
    Проверяет что пользователь верифицирован.
    Если нет — отправляет сообщение с предложением пройти верификацию.
    Возвращает True если верифицирован, False если нет.
    """
    verified = await is_user_verified_by_internal_id(internal_id)
    if verified:
        return True

    kb = InlineKeyboard(rows=[
        [InlineButton("Закончить верификацию", callback_data="register")],
        [InlineButton("Мой профиль", callback_data="profile")],
    ])
    await _send_or_edit(
        ctx, chat_id,
        "Доступ к корзине и покупкам открыт только после верификации.\n\n"
        "Подтвердите телефон и укажите ИНН.",
        keyboard=kb,
    )
    return False


# ══════════════════════════════════════════════════════════════════════
#  УТИЛИТА: поиск внешнего товара по operatorId/typeId
# ══════════════════════════════════════════════════════════════════════

def _find_ext_product(products: dict, operator_id, type_id):
    """Ищет товар в дереве {vendor: {term: {...}}} по operatorId/typeId.
    Возвращает (vendor_name, item_dict) или (None, None).
    """
    try:
        op = int(operator_id)
        tp = int(type_id)
    except Exception:
        return None, None

    for vendor, terms in (products or {}).items():
        if not isinstance(terms, dict):
            continue
        for _, item in terms.items():
            try:
                if int(item.get("operatorId") or 0) == op and int(item.get("typeId") or 0) == tp:
                    return str(vendor), item
            except Exception:
                continue
    return None, None


# ══════════════════════════════════════════════════════════════════════
#  УТИЛИТА: парсеры SKU для внешних товаров
# ══════════════════════════════════════════════════════════════════════
# SKU формат: "ofd_ext:operatorId:typeId[:orderId]"
#             "service_ext:...", "service_ord_ext:...", "edo_ext:..."

def _parse_ext_ofd_ids(sku: str):
    s = (sku or "").lower().strip()
    if not s.startswith("ofd_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    return parts[1], parts[2], (parts[3] if len(parts) >= 4 and parts[3] else None)


def _parse_ext_service_ids(sku: str):
    s = (sku or "").lower().strip()
    if not s.startswith("service_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    return parts[1], parts[2], (parts[3] if len(parts) >= 4 and parts[3] else None)


def _parse_ext_service_order_ids(sku: str):
    s = (sku or "").lower().strip()
    if not s.startswith("service_ord_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    return parts[1], parts[2], (parts[3] if len(parts) >= 4 and parts[3] else None)


def _parse_ext_edo_ids(sku: str):
    s = (sku or "").lower().strip()
    if not s.startswith("edo_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    return parts[1], parts[2], (parts[3] if len(parts) >= 4 and parts[3] else None)


def _parse_ext_any_ids(sku: str):
    """Возвращает (prefix, operator_id, type_id, ext_order_id) или None."""
    for prefix, parser in (
        ("ofd_ext", _parse_ext_ofd_ids),
        ("service_ext", _parse_ext_service_ids),
        ("service_ord_ext", _parse_ext_service_order_ids),
        ("edo_ext", _parse_ext_edo_ids),
    ):
        ids = parser(sku)
        if ids:
            op, tp, oid = ids
            return prefix, op, tp, oid
    return None


# ══════════════════════════════════════════════════════════════════════
#  КОРЗИНА: базовая функция добавления (нужна внешним товарам)
#  Полная корзина будет в подзадаче 5, но _cart_try_add_line нужна уже здесь
# ══════════════════════════════════════════════════════════════════════

def _stock_key_for_sku(sku: str):
    """Возвращает (kind, key) для проверки наличия по SKU.
    - ('pool', 'ofd_first'|'ofd_platform') — наши пулы кодов
    - ('ext', 'prefix:op:tp') — внешние коды
    - ('equip', 'id') — оборудование
    - (None, None) — товар без ограничения по наличию
    """
    s = (sku or "").lower().strip()

    # внешние товары
    ids = _parse_ext_any_ids(s)
    if ids:
        prefix, op, tp, _oid = ids
        return "ext", f"{prefix}:{op}:{tp}"

    # оборудование
    if s.startswith("equip_"):
        try:
            eid = int(s.split("_", 1)[1])
            return "equip", str(eid)
        except Exception:
            return None, None

    # наши пулы кодов
    if "ofd_platform" in s or "platform" in s:
        return "pool", "ofd_platform"
    if s == "ofd_first" or s.startswith("ofd_first"):
        return "pool", "ofd_first"
    if s.startswith("ofd"):
        return "pool", "ofd_first"

    return None, None


def _cart_sum_by_stock_key(cart: list, kind: str, key: str, exclude_idx=None) -> int:
    """Считает сколько единиц товара с данным stock_key уже в корзине."""
    total = 0
    for i, it in enumerate(cart or []):
        if exclude_idx is not None and i == exclude_idx:
            continue
        k_kind, k_key = _stock_key_for_sku(str(it.get("sku") or ""))
        if k_kind == kind and k_key == key:
            total += int(it.get("qty") or 1)
    return total


async def _available_for_stock_key(kind: str, key: str, *, force: bool = False) -> int:
    """Сколько доступно сейчас по stock_key."""
    if kind == "pool":
        return await get_free_codes_count(key)

    if kind == "equip":
        try:
            eid = int(key)
        except Exception:
            return 0
        cnt = await get_equipment_available_count(eid)
        if cnt is None:
            return 10**9
        try:
            return int(cnt or 0)
        except Exception:
            return 0

    if kind == "ext":
        parts = (key or "").split(":")
        if len(parts) < 3:
            return 0
        prefix = parts[0]
        op, tp = parts[1], parts[2]

        if prefix == "service_ext":
            products = refresh_ext_service_products(force=force)
        elif prefix == "service_ord_ext":
            return 10**9  # прошивки без ограничения
        elif prefix == "edo_ext":
            products = refresh_ext_edo_products(force=force)
        else:
            products = refresh_ext_ofd_products(force=force)

        _vendor, prod = _find_ext_product(products, op, tp)
        try:
            cnt = (prod or {}).get("count")
            if cnt is None:
                return 10**9
            return int(cnt or 0)
        except Exception:
            return 0

    return 10**9


async def _cart_try_add_line(cart: list, line: dict) -> tuple:
    """Добавить 1 шт. товара в корзину (или увеличить qty существующей строки).
    Проверяет наличие. Возвращает (added: bool, error_msg: str|None).
    """
    sku = str(line.get("sku") or "").strip()
    if not sku:
        return False, "Некорректный товар"

    kind, key = _stock_key_for_sku(sku)
    add_qty = int(line.get("qty") or 1)

    # Ищем существующую строку с тем же SKU
    same_idx = None
    for i, it in enumerate(cart):
        if str(it.get("sku") or "") == sku:
            same_idx = i
            break

    # Проверяем наличие
    if kind and key:
        avail = await _available_for_stock_key(kind, key, force=True)
        already = _cart_sum_by_stock_key(cart, kind, key)
        if already + add_qty > avail:
            return False, f"Доступно: {avail} шт."

    # Мержим или добавляем
    if same_idx is not None:
        cart[same_idx]["qty"] = int(cart[same_idx].get("qty") or 1) + add_qty
    else:
        line = dict(line)
        line["qty"] = add_qty
        cart.append(line)

    return True, None


async def _cart_try_inc(cart: list, idx: int) -> tuple:
    """Увеличить qty строки idx на 1 с проверкой наличия.
    Возвращает (changed: bool, error_msg: str|None).
    """
    if not (0 <= idx < len(cart)):
        return False, None

    it = cart[idx]
    cur = int(it.get("qty") or 1)
    sku = str(it.get("sku") or "")
    kind, key = _stock_key_for_sku(sku)
    if not kind or not key:
        it["qty"] = cur + 1
        return True, None

    avail = await _available_for_stock_key(kind, key, force=True)
    already_other = _cart_sum_by_stock_key(cart, kind, key, exclude_idx=idx)
    if cur + 1 + already_other > avail:
        return False, f"Доступно: {avail} шт."
    it["qty"] = cur + 1
    return True, None


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 2: Главное меню магазина
# ══════════════════════════════════════════════════════════════════════

async def show_shop(ctx: MaxBotContext, chat_id: str,
                    cb: Optional[MaxCallback] = None) -> None:
    """
    Точка входа в магазин.  callback_data = "buy"

    Показываем кнопки разделов:
      - Коды ОФД
      - Лицензии и прошивки
      - ЭДО
      - ФН (если категория есть в БД equipment)
      - Торговое оборудование
      - Консультация
      - В меню
    """
    # Запрашиваем категории оборудования — ищем "ФН" для отдельной кнопки
    cats0_eq = await get_equipment_categories(is_used=0, include_fn=True)
    ctx.user_data["eq_cats_0"] = cats0_eq

    fn_category_idx = None
    for i, name in enumerate(cats0_eq or []):
        if name == "ФН":
            fn_category_idx = i
            break

    # Собираем кнопки
    rows = [
        [InlineButton("Коды ОФД", callback_data="services_ofd")],
    ]

    # Если есть категория ФН — вставляем кнопку перед оборудованием
    if fn_category_idx is not None:
        rows.append([InlineButton("ФН", callback_data=f"eq_cat:0:{fn_category_idx}")])

    rows.append([InlineButton("ЭДО", callback_data="services_edo")])
    rows.append([InlineButton("Торговое оборудование", callback_data="shop_equipment")])
    rows.append([InlineButton("Лицензии и прошивки", callback_data="services_service")])
    rows.append([InlineButton("Бумага", callback_data="shop_paper")])
    rows.append([InlineButton("Консультация", callback_data="shop_consult:shop")])
    rows.append([InlineButton("В меню", callback_data="back_to_main")])

    kb = InlineKeyboard(rows=rows)

    await _send_or_edit(ctx, chat_id, "Выберите раздел:", keyboard=kb)


async def shop_consult_cb(ctx: MaxBotContext, cb: MaxCallback,
                          chat_id: str) -> None:
    """
    callback_data = "shop_consult:{section}"

    Пока просто показываем текст «свяжитесь с нами» + кнопку назад.
    В будущем можно привязать к клиентскому чату.
    """

    # Определяем откуда пришли, чтобы кнопка «Назад» вела правильно
    section = (cb.payload or "").split(":", 1)[-1] if ":" in (cb.payload or "") else "shop"
    back_cb = "buy"  # по умолчанию — в магазин

    text = (
        "По вопросам консультации свяжитесь с нами:\n\n"
        "Напишите ваш вопрос в свободной форме, "
        "и мы свяжемся с вами в ближайшее время."
    )

    kb = InlineKeyboard(rows=[
        [InlineButton("Назад", callback_data=back_cb)],
        [InlineButton("В меню", callback_data="back_to_main")],
    ])

    await _send_or_edit(ctx, chat_id, text, keyboard=kb)


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 3а: ОФД — список провайдеров, тарифы, добавление в корзину
# ══════════════════════════════════════════════════════════════════════

async def show_services_ofd(ctx: MaxBotContext, cb: MaxCallback,
                            chat_id: str) -> None:
    """
    callback_data = "services_ofd"

    Показывает:
    1) Встроенные ОФД (Платформа, Первый) — из PRODUCTS
    2) Внешние провайдеры — из кэша extern_ofd
    """

    rows = [
        [InlineButton(
            f"ОФД Платформа — {PRODUCTS['ofd_platform']['price']} руб",
            callback_data="product_ofd_platform",
        )],
        [InlineButton(
            f"Первый ОФД — {PRODUCTS['ofd_first']['price']} руб",
            callback_data="product_ofd_first",
        )],
    ]

    # Внешние провайдеры
    products = refresh_ext_ofd_products()
    for idx, provider in enumerate(products.keys()):
        rows.append([InlineButton(provider, callback_data=f"ext_ofd_p:{idx}")])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:ofd")])
    rows.append([InlineButton("Назад", callback_data="buy")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, "Коды ОФД\n\nВыберите провайдера:", keyboard=kb)


async def ext_ofd_provider_cb(ctx: MaxBotContext, cb: MaxCallback,
                               chat_id: str) -> None:
    """
    callback_data = "ext_ofd_p:{provider_idx}"

    Показывает тарифы выбранного ОФД-провайдера с ценами.
    """

    try:
        provider_idx = int((cb.payload or "").split(":", 1)[1])
    except Exception:
        return await show_shop(ctx, chat_id)

    products = refresh_ext_ofd_products()
    providers = list(products.keys())

    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_shop(ctx, chat_id)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("ofd", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineButton(
            f"{term_name} — {price} руб",
            callback_data=f"ext_ofd_add:{provider_idx}:{term_idx}",
        )])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:ofd")])
    rows.append([InlineButton("Назад", callback_data="services_ofd")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, f"ОФД «{provider}»\nВыберите срок:", keyboard=kb)


async def ext_ofd_add_cb(ctx: MaxBotContext, cb: MaxCallback,
                          chat_id: str, internal_id: int) -> None:
    """
    callback_data = "ext_ofd_add:{provider_idx}:{term_idx}"

    Добавляет внешний ОФД-код в корзину.
    """
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    try:
        parts = (cb.payload or "").split(":")
        provider_idx = int(parts[1])
        term_idx = int(parts[2])
    except Exception:
        return

    products = refresh_ext_ofd_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_ofd(ctx, cb, chat_id)

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        return await ext_ofd_provider_cb(ctx, cb, chat_id)

    term_name, item = terms[term_idx]

    sell_price = await ext_sell_price("ofd", item["operatorId"], item["typeId"], item["price"])
    sku = f"ofd_ext:{item['operatorId']}:{item['typeId']}"

    cart = ctx.user_data.setdefault("cart", [])
    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": item["name"],
        "price": int(sell_price),
        "qty": 1,
        "meta": {
            "operatorId": item["operatorId"],
            "typeId": item["typeId"],
            "source_price": item["price"],
            "months_number": item.get("months_number"),
            "vendor_name": provider,
            "term_name": term_name,
            "available": item.get("count"),
        },
    })


    if not added:
        await _send_or_edit(ctx, chat_id, f"Нельзя добавить. {msg or ''}")
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("Корзина", callback_data="cart")],
        [InlineButton("К магазину", callback_data="buy")],
        [InlineButton("В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, f"{item['name']} добавлен в корзину.", keyboard=kb)


async def add_builtin_product_cb(ctx: MaxBotContext, cb: MaxCallback,
                                  chat_id: str, internal_id: int) -> None:
    """
    callback_data = "product_{sku}"  (например product_ofd_platform, product_ofd_first)

    Добавляет встроенный товар из PRODUCTS в корзину.
    """
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    sku = (cb.payload or "").split("product_", 1)[-1]
    p = PRODUCTS.get(sku)
    if not p:
        await _send_or_edit(ctx, chat_id, "Товар не найден.")
        return

    cart = ctx.user_data.setdefault("cart", [])
    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": p["name"],
        "price": p["price"],
        "qty": 1,
        "meta": (
            {"equipment_id": await get_equipment_id_by_name_category(p["name"], "fn")}
            if str(sku).startswith("fn_") else None
        ),
    })


    if not added:
        await _send_or_edit(ctx, chat_id, f"Нельзя добавить. {msg or ''}")
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("Корзина", callback_data="cart")],
        [InlineButton("К магазину", callback_data="buy")],
        [InlineButton("В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, f"{p['name']} добавлен в корзину.", keyboard=kb)


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 3б: Лицензии / Сервисы (service + service_orders)
# ══════════════════════════════════════════════════════════════════════

async def show_services_service(ctx: MaxBotContext, cb: MaxCallback,
                                 chat_id: str) -> None:
    """
    callback_data = "services_service"

    Список провайдеров лицензий/прошивок + service_orders.
    """

    rows = []
    products = refresh_ext_service_products()
    for idx, title in enumerate(products.keys()):
        rows.append([InlineButton(title, callback_data=f"ext_srv_p:{idx}")])

    products_for_orders = refresh_ext_service_orders_products()
    for idx, title in enumerate(products_for_orders.keys()):
        rows.append([InlineButton(title, callback_data=f"ext_srv_ord_p:{idx}")])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:service")])
    rows.append([InlineButton("Назад", callback_data="buy")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, "Лицензии и прошивки\n\nВыберите продукт:", keyboard=kb)


async def ext_srv_provider_cb(ctx: MaxBotContext, cb: MaxCallback,
                               chat_id: str) -> None:
    """callback_data = "ext_srv_p:{idx}" — тарифы сервис-провайдера."""

    try:
        provider_idx = int((cb.payload or "").split(":", 1)[1])
    except Exception:
        return

    products = refresh_ext_service_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_service(ctx, cb, chat_id)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("service", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineButton(
            f"{term_name} — {price} руб",
            callback_data=f"ext_srv_add:{provider_idx}:{term_idx}",
        )])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:service")])
    rows.append([InlineButton("Назад", callback_data="services_service")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, f"Купить «{provider}»\nВыберите вариант:", keyboard=kb)


async def ext_srv_add_cb(ctx: MaxBotContext, cb: MaxCallback,
                          chat_id: str, internal_id: int) -> None:
    """callback_data = "ext_srv_add:{provider_idx}:{term_idx}" — добавить сервис в корзину."""
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    try:
        parts = (cb.payload or "").split(":")
        provider_idx = int(parts[1])
        term_idx = int(parts[2])
    except Exception:
        return

    products = refresh_ext_service_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        return

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        return

    term_name, item = terms[term_idx]
    sell_price = await ext_sell_price("service", item["operatorId"], item["typeId"], item["price"])
    sku = f"service_ext:{item['operatorId']}:{item['typeId']}"

    cart = ctx.user_data.setdefault("cart", [])
    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": item["name"],
        "price": int(sell_price),
        "qty": 1,
        "meta": {
            "operatorId": item["operatorId"],
            "typeId": item["typeId"],
            "source_price": item["price"],
            "months_number": item.get("months_number"),
            "vendor_name": provider,
            "term_name": term_name,
            "available": item.get("count"),
        },
    })


    if not added:
        await _send_or_edit(ctx, chat_id, f"Нельзя добавить. {msg or ''}")
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("Корзина", callback_data="cart")],
        [InlineButton("К магазину", callback_data="buy")],
        [InlineButton("В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, f"{item['name']} добавлен в корзину.", keyboard=kb)


async def ext_srv_ord_provider_cb(ctx: MaxBotContext, cb: MaxCallback,
                                   chat_id: str) -> None:
    """callback_data = "ext_srv_ord_p:{idx}" — тарифы сервис-заказов (прошивки)."""

    try:
        provider_idx = int((cb.payload or "").split(":", 1)[1])
    except Exception:
        return

    products = refresh_ext_service_orders_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_service(ctx, cb, chat_id)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("services_for_orders", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineButton(
            f"{term_name} — {price} руб",
            callback_data=f"ext_srv_ord_add:{provider_idx}:{term_idx}",
        )])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:service_orders")])
    rows.append([InlineButton("Назад", callback_data="services_service")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, f"Купить «{provider}»\nВыберите вариант:", keyboard=kb)


async def ext_srv_ord_add_cb(ctx: MaxBotContext, cb: MaxCallback,
                              chat_id: str, internal_id: int) -> None:
    """callback_data = "ext_srv_ord_add:{provider_idx}:{term_idx}" — добавить прошивку в корзину."""
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    try:
        parts = (cb.payload or "").split(":")
        provider_idx = int(parts[1])
        term_idx = int(parts[2])
    except Exception:
        return

    products = refresh_ext_service_orders_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        return

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        return

    term_name, item = terms[term_idx]
    sell_price = await ext_sell_price("services_for_orders", item["operatorId"], item["typeId"], item["price"])
    sku = f"service_ord_ext:{item['operatorId']}:{item['typeId']}"

    cart = ctx.user_data.setdefault("cart", [])
    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": item["name"],
        "price": int(sell_price),
        "qty": 1,
        "meta": {
            "operatorId": item["operatorId"],
            "typeId": item["typeId"],
            "source_price": item["price"],
            "months_number": item.get("months_number"),
            "vendor_name": provider,
            "term_name": term_name,
            "available": item.get("count"),
        },
    })


    if not added:
        await _send_or_edit(ctx, chat_id, f"Нельзя добавить. {msg or ''}")
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("Корзина", callback_data="cart")],
        [InlineButton("К магазину", callback_data="buy")],
        [InlineButton("В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, f"{item['name']} добавлен в корзину.", keyboard=kb)


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 3в: ЭДО
# ══════════════════════════════════════════════════════════════════════

async def show_services_edo(ctx: MaxBotContext, cb: MaxCallback,
                             chat_id: str) -> None:
    """callback_data = "services_edo" — список ЭДО-провайдеров."""

    products = refresh_ext_edo_products()
    rows = []
    for idx, title in enumerate(products.keys()):
        rows.append([InlineButton(title, callback_data=f"ext_edo_p:{idx}")])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:edo")])
    rows.append([InlineButton("Назад", callback_data="buy")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, "ЭДО\n\nВыберите продукт:", keyboard=kb)


async def ext_edo_provider_cb(ctx: MaxBotContext, cb: MaxCallback,
                               chat_id: str) -> None:
    """callback_data = "ext_edo_p:{idx}" — тарифы ЭДО-провайдера."""

    try:
        provider_idx = int((cb.payload or "").split(":", 1)[1])
    except Exception:
        return

    products = refresh_ext_edo_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_edo(ctx, cb, chat_id)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("edo", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineButton(
            f"{term_name} — {price} руб",
            callback_data=f"ext_edo_add:{provider_idx}:{term_idx}",
        )])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:edo")])
    rows.append([InlineButton("Назад", callback_data="services_edo")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, f"ЭДО «{provider}»\nВыберите вариант:", keyboard=kb)


async def ext_edo_add_cb(ctx: MaxBotContext, cb: MaxCallback,
                          chat_id: str, internal_id: int) -> None:
    """callback_data = "ext_edo_add:{provider_idx}:{term_idx}" — добавить ЭДО в корзину."""
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    try:
        parts = (cb.payload or "").split(":")
        provider_idx = int(parts[1])
        term_idx = int(parts[2])
    except Exception:
        return

    products = refresh_ext_edo_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        return

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        return

    term_name, item = terms[term_idx]
    sell_price = await ext_sell_price("edo", item["operatorId"], item["typeId"], item["price"])
    sku = f"edo_ext:{item['operatorId']}:{item['typeId']}"

    cart = ctx.user_data.setdefault("cart", [])
    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": item["name"],
        "price": int(sell_price),
        "qty": 1,
        "meta": {
            "operatorId": item["operatorId"],
            "typeId": item["typeId"],
            "source_price": item["price"],
            "months_number": item.get("months_number"),
            "vendor_name": provider,
            "term_name": term_name,
            "available": item.get("count"),
        },
    })


    if not added:
        await _send_or_edit(ctx, chat_id, f"Нельзя добавить. {msg or ''}")
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("Корзина", callback_data="cart")],
        [InlineButton("К магазину", callback_data="buy")],
        [InlineButton("В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, f"{item['name']} добавлен в корзину.", keyboard=kb)


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 4: Оборудование — 3-уровневая навигация
# ══════════════════════════════════════════════════════════════════════


def _equipment_image_path(image_name: str) -> str:
    """Путь к фото оборудования — лежат в bot/images/."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "images", image_name)


async def show_shop_equipment(ctx: MaxBotContext, chat_id: str,
                               cb: Optional[MaxCallback] = None) -> None:
    """
    callback_data = "shop_equipment"

    Уровень 1: список категорий нового оборудования + кнопка «Б/у».
    Загружает категории из БД (get_equipment_categories).
    """
    cats0 = await get_equipment_categories(is_used=0)
    ctx.user_data["eq_cats_0"] = cats0

    _paper = {"Кассовые ленты", "Этикетки"}
    rows = []
    for i, cat in enumerate(cats0 or []):
        if str(cat) not in _paper:
            rows.append([InlineButton(str(cat), callback_data=f"eq_cat:0:{i}")])

    rows.append([InlineButton("Б/у", callback_data="eq_root:1")])
    rows.append([InlineButton("Консультация", callback_data="shop_consult:equipment")])
    rows.append([InlineButton("Назад", callback_data="buy")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, "Торговое оборудование\n\nВыберите категорию:", keyboard=kb)


async def show_shop_paper(ctx: MaxBotContext, chat_id: str,
                          cb: Optional[MaxCallback] = None) -> None:
    """Раздел «Бумага»: категории «Кассовые ленты» и «Этикетки»."""
    cats0 = await get_equipment_categories(is_used=0)
    ctx.user_data["eq_cats_0"] = cats0
    paper_categories = {"Кассовые ленты", "Этикетки"}

    rows = []
    for i, cat in enumerate(cats0 or []):
        if str(cat) in paper_categories:
            rows.append([InlineButton(str(cat), callback_data=f"eq_cat:0:{i}")])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:paper")])
    rows.append([InlineButton("Назад", callback_data="buy")])

    text = "Бумага\n\nВыберите категорию:"
    if not any(str(cat) in paper_categories for cat in (cats0 or [])):
        text = "Бумага\n\nКатегории «Кассовые ленты» и «Этикетки» пока не добавлены."

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, text, keyboard=kb)


async def eq_root_cb(ctx: MaxBotContext, cb: MaxCallback,
                      chat_id: str) -> None:
    """
    callback_data = "eq_root:{is_used}"

    Переключение между новым (0) и б/у (1) оборудованием.
    """

    try:
        is_used = int((cb.payload or "").split(":", 1)[1])
    except Exception:
        is_used = 0

    if is_used == 0:
        return await show_shop_equipment(ctx, chat_id)

    # Б/у — загружаем категории
    cats = await get_equipment_categories(is_used=1)
    ctx.user_data["eq_cats_1"] = cats

    rows = []
    for i, cat in enumerate(cats or []):
        rows.append([InlineButton(str(cat), callback_data=f"eq_cat:1:{i}")])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:equipment")])
    rows.append([InlineButton("Назад", callback_data="shop_equipment")])

    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, "Б/у оборудование\n\nВыберите категорию:", keyboard=kb)


async def eq_category_cb(ctx: MaxBotContext, cb: MaxCallback,
                          chat_id: str) -> None:
    """
    callback_data = "eq_cat:{is_used}:{category_idx}"

    Уровень 2: список товаров в выбранной категории.
    """

    try:
        _, is_used_s, idx_s = (cb.payload or "").split(":", 2)
        is_used = int(is_used_s)
        idx = int(idx_s)
    except Exception:
        return await show_shop_equipment(ctx, chat_id)

    # Берём категории из кэша user_data или загружаем из БД
    cats = ctx.user_data.get(f"eq_cats_{is_used}") or await get_equipment_categories(is_used=is_used)
    ctx.user_data[f"eq_cats_{is_used}"] = cats

    if idx < 0 or idx >= len(cats):
        return await show_shop_equipment(ctx, chat_id)

    category = cats[idx]
    items = await get_equipment_items(str(category), is_used=is_used)

    rows = []
    for it in items or []:
        try:
            item_id = int(it.get("id"))
        except Exception:
            continue
        name = str(it.get("name") or "Товар")
        price = int(it.get("price") or 0)
        rows.append([InlineButton(
            f"{name} — {price} руб",
            callback_data=f"eq_view:{is_used}:{idx}:{item_id}",
        )])

    rows.append([InlineButton("Консультация", callback_data="shop_consult:equipment")])
    back_cb = "eq_root:1" if is_used else "shop_equipment"
    rows.append([InlineButton("Назад", callback_data=back_cb)])

    title_icon = "Б/у" if is_used else ""
    text = f"{title_icon} {category}\n\nВыберите товар:".strip()
    kb = InlineKeyboard(rows=rows)
    await _send_or_edit(ctx, chat_id, text, keyboard=kb)


async def eq_view_cb(ctx: MaxBotContext, cb: MaxCallback,
                      chat_id: str) -> None:
    """
    callback_data = "eq_view:{is_used}:{cat_idx}:{item_id}"

    Уровень 3: карточка товара — название, цена, наличие, фото.
    Кнопки: Добавить / Консультация / Назад.
    """

    try:
        _, is_used_s, idx_s, item_id_s = (cb.payload or "").split(":", 3)
        is_used = int(is_used_s)
        idx = int(idx_s)
        item_id = int(item_id_s)
    except Exception:
        return

    it = await get_equipment_by_id(item_id)
    if not it:
        await _send_or_edit(ctx, chat_id, "Товар не найден.")
        return

    name = str(it.get("name") or "Товар")
    price = int(it.get("price") or 0)
    image_name = (it.get("image_name") or "").strip()

    # Наличие
    avail = await get_equipment_available_count(item_id)
    if avail is None:
        stock_line = "В наличии: без ограничений"
        can_buy = True
    else:
        stock_line = f"В наличии: {int(avail)}"
        can_buy = int(avail) > 0

    caption = f"{name}\nЦена: {price} руб\n{stock_line}"

    # Кнопки
    if can_buy:
        btn_add = InlineButton("Добавить в корзину", callback_data=f"eq_add:{is_used}:{idx}:{item_id}")
    else:
        btn_add = InlineButton("Нет в наличии", callback_data="cart_noop")

    kb = InlineKeyboard(rows=[
        [btn_add],
        [InlineButton("Консультация", callback_data="shop_consult:equipment")],
        [InlineButton("Назад", callback_data=f"eq_cat:{is_used}:{idx}")],
    ])

    # Пробуем отправить с фото (send_photo принимает bytes)
    img_path = _equipment_image_path(str(image_name)) if image_name else ""
    if image_name and os.path.exists(img_path):
        try:
            with open(img_path, "rb") as f:
                photo_bytes = f.read()
            await ctx.api.send_photo(
                chat_id, photo_bytes,
                caption=caption, filename=image_name, keyboard=kb,
            )
            return
        except Exception:
            logger.exception("eq_view_cb: не удалось отправить фото %s", img_path)

    # Без фото — просто текст
    if image_name and not os.path.exists(img_path):
        caption += "\n\n(Фото не найдено)"
    await _send_or_edit(ctx, chat_id, caption, keyboard=kb)


async def eq_add_cb(ctx: MaxBotContext, cb: MaxCallback,
                     chat_id: str, internal_id: int) -> None:
    """
    callback_data = "eq_add:{is_used}:{cat_idx}:{item_id}"

    Добавляет оборудование в корзину с проверкой верификации и наличия.
    """
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return


    try:
        _, is_used_s, idx_s, item_id_s = (cb.payload or "").split(":", 3)
        item_id = int(item_id_s)
    except Exception:
        return

    it = await get_equipment_by_id(item_id)
    if not it:
        await _send_or_edit(ctx, chat_id, "Товар не найден.")
        return

    # Проверка наличия
    avail = await get_equipment_available_count(item_id)
    if avail is not None and int(avail) < 1:
        await _send_or_edit(ctx, chat_id, "Нет в наличии.")
        return

    name = str(it.get("name") or "Товар")
    price = int(it.get("price") or 0)
    sku = f"equip_{item_id}"

    cart = ctx.user_data.setdefault("cart", [])
    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": name,
        "price": price,
        "qty": 1,
        "meta": {"equipment_id": item_id, "is_used": int(it.get("is_used") or 0)},
    })

    if not added:
        await _send_or_edit(ctx, chat_id, f"Нельзя добавить. {msg or ''}")
        return

    kb = InlineKeyboard(rows=[
        [InlineButton("Корзина", callback_data="cart")],
        [InlineButton("К магазину", callback_data="buy")],
        [InlineButton("В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, f"{name} добавлен в корзину.", keyboard=kb)


async def cart_noop_cb(ctx: MaxBotContext, cb: MaxCallback,
                        chat_id: str) -> None:
    """callback_data = "cart_noop" — кнопка-заглушка (кол-во / нет в наличии)."""


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 5: Корзина — показ, +-qty, удаление, валидация наличия
# ══════════════════════════════════════════════════════════════════════

def _cart_total(cart: list) -> int:
    """Сумма корзины в рублях."""
    return sum(int(x.get("price") or 0) * int(x.get("qty") or 1) for x in (cart or []))


def _cart_item_title(it: dict) -> str:
    """Название позиции — ищем в разных ключах."""
    return it.get("item_title") or it.get("title") or it.get("name") or "Товар"


def _format_cart_text(cart: list, notice: str = None) -> str:
    """Текст корзины: пронумерованные позиции + итого."""
    if not cart:
        return "Корзина пуста."

    lines = []
    if notice:
        lines.append(notice)
        lines.append("")
    lines.append("Корзина\n")

    for idx, it in enumerate(cart, start=1):
        title = _cart_item_title(it)
        price = int(it.get("price") or 0)
        qty = int(it.get("qty") or 1)
        lines.append(f"{idx}) {title} — {qty} шт. x {price} руб = {qty * price} руб")

    total = _cart_total(cart)
    lines.append(f"\nИтого: {total} руб")
    lines.append("\nМеняй количество кнопками -/+ или удаляй строку кнопкой X")
    return "\n".join(lines)


def _kb_cart(cart: list) -> InlineKeyboard:
    """Клавиатура корзины: ряд ➖ qty ➕ 🗑 для каждой позиции + Оформить/Очистить/Меню."""
    rows = []

    for i, it in enumerate(cart or []):
        qty = int(it.get("qty") or 1)
        rows.append([
            InlineButton("-", callback_data=f"cart_dec:{i}"),
            InlineButton(f" {qty} ", callback_data="cart_noop"),
            InlineButton("+", callback_data=f"cart_inc:{i}"),
            InlineButton("X", callback_data=f"del_{i}"),
        ])

    if cart:
        rows.append([InlineButton("Оформить", callback_data="checkout")])
        rows.append([InlineButton("Очистить корзину", callback_data="clear_cart")])

    rows.append([InlineButton("В меню", callback_data="back_to_main")])
    return InlineKeyboard(rows=rows)


async def show_cart(ctx: MaxBotContext, cb: Optional[MaxCallback],
                    chat_id: str, internal_id: int = 0) -> None:
    """
    callback_data = "cart"

    Показывает содержимое корзины с кнопками управления.
    """
    if internal_id and not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    cart = ctx.user_data.get("cart") or []

    if not cart:
        kb = InlineKeyboard(rows=[
            [InlineButton("К магазину", callback_data="buy")],
            [InlineButton("В меню", callback_data="back_to_main")],
        ])
        await _send_or_edit(ctx, chat_id, "Корзина пуста.", keyboard=kb)
        return

    text = _format_cart_text(cart)
    kb = _kb_cart(cart)
    await _send_or_edit(ctx, chat_id, text, keyboard=kb)


async def cart_inc_cb(ctx: MaxBotContext, cb: MaxCallback,
                      chat_id: str, internal_id: int) -> None:
    """
    callback_data = "cart_inc:{idx}"

    Увеличивает количество товара на 1 с проверкой наличия.
    """
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    try:
        idx = int((cb.payload or "").split(":", 1)[1])
    except Exception:
        return await show_cart(ctx, None, chat_id, internal_id)

    cart = ctx.user_data.get("cart") or []
    if not (0 <= idx < len(cart)):
        return await show_cart(ctx, None, chat_id, internal_id)

    changed, msg = await _cart_try_inc(cart, idx)

    if not changed:
        # Отвечаем на callback с уведомлением (notification)
        await ctx.api.answer_callback(cb.callback_id, notification=msg or "Недостаточно в наличии")
    await show_cart(ctx, None, chat_id, internal_id)


async def cart_dec_cb(ctx: MaxBotContext, cb: MaxCallback,
                      chat_id: str, internal_id: int) -> None:
    """
    callback_data = "cart_dec:{idx}"

    Уменьшает количество на 1. Если qty становится 0 — удаляет строку.
    """
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return


    try:
        idx = int((cb.payload or "").split(":", 1)[1])
    except Exception:
        return await show_cart(ctx, None, chat_id, internal_id)

    cart = ctx.user_data.get("cart") or []
    if 0 <= idx < len(cart):
        cur = int(cart[idx].get("qty") or 1) - 1
        if cur <= 0:
            cart.pop(idx)
        else:
            cart[idx]["qty"] = cur
    ctx.user_data["cart"] = cart
    await show_cart(ctx, None, chat_id, internal_id)


async def remove_from_cart_cb(ctx: MaxBotContext, cb: MaxCallback,
                               chat_id: str, internal_id: int) -> None:
    """
    callback_data = "del_{idx}"

    Удаляет позицию из корзины по индексу.
    """
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return


    try:
        idx = int((cb.payload or "").split("_", 1)[1])
    except Exception:
        return await show_cart(ctx, None, chat_id, internal_id)

    cart = ctx.user_data.get("cart") or []
    if 0 <= idx < len(cart):
        cart.pop(idx)
    ctx.user_data["cart"] = cart
    await show_cart(ctx, None, chat_id, internal_id)


async def clear_cart_cb(ctx: MaxBotContext, cb: MaxCallback,
                         chat_id: str, internal_id: int) -> None:
    """
    callback_data = "clear_cart"

    Очищает корзину полностью.
    """
    if not await _require_verified(ctx, chat_id, internal_id, cb):
        return

    ctx.user_data["cart"] = []
    await show_cart(ctx, None, chat_id, internal_id)


async def _validate_cart_stock(cart: list) -> list:
    """Проверяет наличие всех товаров в корзине.
    Возвращает список проблем: [{"kind", "key", "need", "available"}, ...]
    Пустой список = всё ок.
    """
    req = {}
    for it in cart or []:
        kind, key = _stock_key_for_sku(str(it.get("sku") or ""))
        if not kind or not key:
            continue
        req[(kind, key)] = req.get((kind, key), 0) + int(it.get("qty") or 1)

    problems = []
    for (kind, key), need in req.items():
        avail = await _available_for_stock_key(kind, key, force=True)
        if need > avail:
            problems.append({"kind": kind, "key": key, "need": need, "available": avail})
    return problems


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 6: Checkout + Tinkoff оплата
# ══════════════════════════════════════════════════════════════════════

# ── Константы Tinkoff (из ENV) ────────────────────────────────────────
TINKOFF_TAXATION = os.getenv("TINKOFF_TAXATION", "usn_income").strip()
COMPANY_EMAIL = (os.getenv("COMPANY_EMAIL", "") or "").strip() or None
COMPANY_INN = (os.getenv("COMPANY_INN", "") or "").strip() or None


def _receipt_phone(phone: str | None) -> str | None:
    """Нормализация телефона для чека Tinkoff: -> '7XXXXXXXXXX' или None."""
    if not phone:
        return None
    d = "".join(ch for ch in phone if ch.isdigit())
    if len(d) == 10 and d.startswith("9"):
        d = "7" + d
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    return d if len(d) == 11 and d.startswith("7") else None


def _make_items_from_cart(cart: list[dict]) -> list[dict]:
    """Корзина -> items для Tinkoff Receipt (цены в КОПЕЙКАХ)."""
    items = []
    for it in cart or []:
        name = (it.get("name") or "Услуга")[:128]
        price_rub = int(it.get("price") or 0)
        qty = int(it.get("qty") or 1)
        if qty < 1:
            qty = 1
        price_kop = price_rub * 100
        amount_kop = price_kop * qty
        items.append({
            "Name": name,
            "Price": price_kop,
            "Quantity": qty,
            "Amount": amount_kop,
            "Tax": "none",
            "PaymentMethod": "full_prepayment",
            "PaymentObject": "service",
            "MeasurementUnit": "piece",
        })
    return items


def build_receipt(user: dict, items: list[dict]) -> dict:
    """Формирует объект Receipt для Tinkoff Init.
    user: {"email": "...", "phone": "..."}.
    items: уже в копейках (из _make_items_from_cart).
    """
    buyer_email = (user.get("email") or "").strip() or None
    buyer_phone = _receipt_phone(user.get("phone"))

    rec = {
        "Taxation": TINKOFF_TAXATION,
        "Items": items,
    }
    if buyer_email:
        rec["Email"] = buyer_email
    if buyer_phone:
        rec["Phone"] = buyer_phone
    if COMPANY_EMAIL:
        rec["EmailCompany"] = COMPANY_EMAIL

    total_kop = sum(int(i.get("Amount") or 0) for i in items)
    rec["Payments"] = {"Electronic": total_kop}

    if COMPANY_INN:
        for it in rec["Items"]:
            it.setdefault("SupplierInfo", {"Inn": COMPANY_INN})

    return rec


# ── Бронирование кодов при оформлении заказа ─────────────────────────

async def _cart_to_order_items_reserved(cart: list[dict], user_id: str, tester_mode: bool = False) -> list[dict]:
    """Корзина -> строки заказа с бронированием кодов в БД.
    user_id — internal_id пользователя (из users.id).
    Возвращает список для save_order_items().
    """
    out: list[dict] = []

    products_ofd = refresh_ext_ofd_products()
    products_svc = refresh_ext_service_products()
    products_edo = refresh_ext_edo_products()
    products_svc_ord = refresh_ext_service_orders_products()

    for it in (cart or []):
        sku = str(it.get("sku") or "").strip()
        price = int(it.get("price") or 0)
        qty = int(it.get("qty") or 1)
        if qty < 1:
            qty = 1

        # оборудование
        if sku.startswith("equip_"):
            try:
                eq_id = int(sku.split("equip_", 1)[1])
            except Exception:
                continue
            for _ in range(qty):
                out.append({"equipment_id": eq_id, "qty": 1, "price": price})
            continue

        # наши пуловые коды
        if sku in ("ofd_first", "ofd_platform"):
            for _ in range(qty):
                code_id = await reserve_pool_code_for_user(user_id, sku, sell_price=price)
                if not code_id:
                    raise RuntimeError(f"Нет свободных кодов в пуле {sku}")
                out.append({"code_id": int(code_id), "qty": 1, "price": price})
            continue

        # внешние товары (ofd/service/edo/service_order)
        ids = _parse_ext_any_ids(sku)
        if ids:
            prefix, operator_id, type_id, _ = ids

            if prefix == "service_ext":
                products = products_svc
                ct = "service"
            elif prefix == "edo_ext":
                products = products_edo
                ct = "edo"
            elif prefix == "service_ord_ext":
                products = products_svc_ord
                ct = "service_order"
            else:
                products = products_ofd
                ct = "ofd_other"

            vendor, prod = _find_ext_product(products, str(operator_id), str(type_id))
            source_price = None
            months_number = None
            if isinstance(prod, dict):
                source_price = prod.get("price")
                months_number = prod.get("months_number")

            await asyncio.sleep(1)
            if ct == "service_order":
                for _ in range(qty):
                    code_id = await reserve_external_code_for_user(
                        user_id,
                        code_type=ct,
                        operator_id=int(operator_id) if str(operator_id).isdigit() else None,
                        type_id=int(type_id) if str(type_id).isdigit() else None,
                        vseofd_order_id=None,
                        sell_price=price,
                        source_price=int(source_price) if source_price is not None else None,
                        vendor_name=vendor,
                        months_number=months_number,
                    )
                    out.append({"code_id": int(code_id), "qty": 1, "price": price})
                    await asyncio.sleep(1)
            else:
                for _ in range(qty):
                    code_id = await reserve_pool_code_for_user(
                        user_id, ct, operator_id=operator_id, type_id=type_id, sell_price=price,
                    )
                    if not code_id:
                        if tester_mode:
                            ext_order_id = f"TEST_RESERVE:{prefix}:{operator_id}:{type_id}:{user_id}"
                        else:
                            reserve_ans = await _run_in_thread(
                                reserve_ofd_order, str(operator_id), str(type_id), 1,
                            )
                            ext_order_id = ((reserve_ans or {}).get("orderId") or "").strip()
                            if not ext_order_id:
                                raise RuntimeError(
                                    f"reserve_codes() вернул пустой orderId для {prefix}:{operator_id}/{type_id}"
                                )
                        code_id = await reserve_external_code_for_user(
                            user_id,
                            code_type=ct,
                            operator_id=int(operator_id) if str(operator_id).isdigit() else None,
                            type_id=int(type_id) if str(type_id).isdigit() else None,
                            vseofd_order_id=ext_order_id,
                            sell_price=price,
                            source_price=int(source_price) if source_price is not None else None,
                            vendor_name=vendor,
                            months_number=months_number,
                        )
                        if not code_id:
                            raise RuntimeError("Не удалось создать бронь внешнего кода в БД")
                    out.append({"code_id": int(code_id), "qty": 1, "price": price})
            continue

        # неизвестный sku — пропускаем
        continue

    return out


# ── Выкуп внешних кодов после оплаты ─────────────────────────────────

_EXT_OFD_ORDER_LOCKS: dict[str, asyncio.Lock] = {}


def _get_ext_ofd_lock(order_id: str) -> asyncio.Lock:
    lock = _EXT_OFD_ORDER_LOCKS.get(order_id)
    if lock is None:
        lock = asyncio.Lock()
        _EXT_OFD_ORDER_LOCKS[order_id] = lock
    return lock


async def process_external_ofd_after_payment(order_id: str, user_id: str) -> None:
    """После оплаты: оплачиваем резервы на внешней платформе,
    записываем code_value в БД. Идемпотентно.
    """
    lock = _get_ext_ofd_lock(order_id)
    async with lock:
        try:
            _ = int(order_id)
            _ = int(user_id)
        except (ValueError, TypeError):
            return

        items = await get_order_reserved_external_codes(order_id)
        if not items:
            return

        products_ofd = refresh_ext_ofd_products(force=True)
        products_svc = refresh_ext_service_products(force=True)
        products_edo = refresh_ext_edo_products(force=True)

        for it in items:
            vse_id = (it.get("vseofd_order_id") or "").strip()
            if not vse_id:
                continue

            if vse_id.startswith("TEST_RESERVE:"):
                _, prefix, op_id, tp_id, reserve_user_id = (vse_id.split(":", 4) + ["", "", "", "", ""])[:5]
                code_value = f"TEST-{prefix.upper()}-{op_id}-{tp_id}-{order_id}-{reserve_user_id}"
            else:
                pay_ans = await _run_in_thread(pay_reserved_ofd_order, vse_id)
                codes = (pay_ans or {}).get("codes") or []
                if not codes:
                    continue

                code_value = str(codes[0] or "").strip()
            if not code_value:
                continue

            ct = str(it.get("code_type") or "").strip().lower()
            operator_id = str(it.get("operator_id") or "").strip()
            type_id = str(it.get("type_id") or "").strip()

            if ct == "service":
                products = products_svc
                ct_arg = "service"
            elif ct == "edo":
                products = products_edo
                ct_arg = "edo"
            else:
                products = products_ofd
                ct_arg = "ofd_other"

            vendor, prod = _find_ext_product(products, operator_id, type_id)
            source_price = None
            months_number = None
            if isinstance(prod, dict):
                source_price = prod.get("price")
                months_number = prod.get("months_number")

            await save_external_code_for_order_item(
                order_id=order_id,
                order_item_id=int(it["order_item_id"]),
                user_id=user_id,
                code_value=code_value,
                source_price=source_price,
                sell_price=it.get("sell_price") or it.get("price"),
                vendor_name=vendor,
                months_number=months_number,
                code_type=ct_arg,
            )




# ── Текст успешной оплаты ────────────────────────────────────────────

async def _build_payment_success_text(
    order_row: dict, items: list[dict], codes: dict,
    user_first_name: str, detailed_codes: list[dict] | None = None,
) -> str:
    """Формирует текст 'Оплата подтверждена' с перечнем кодов."""
    lines: list[str] = ["Оплата подтверждена ✅"]

    has_equip = any(i.get("equipment_id") is not None for i in (items or []))
    has_ofd_first = any((i.get("code_type") or "").lower() == "ofd_first" for i in (items or []))
    has_ofd_platform = any((i.get("code_type") or "").lower() == "ofd_platform" for i in (items or []))
    has_ext_codes = any((i.get("code_type") or "").lower() in ("ofd_other", "service", "edo") for i in (items or []))
    has_ext_serv_orders = any((i.get("code_type") or "").lower() == "service_order" for i in (items or []))

    if detailed_codes:
        lines.append("")
        lines.append("Ваши коды:")
        for r in detailed_codes:
            code_val = str(
                (r.get("code_value") if isinstance(r, dict) else None)
                or (r.get("code") if isinstance(r, dict) else None)
                or (r.get("value") if isinstance(r, dict) else None)
                or ""
            ).strip()
            if not code_val:
                continue

            name = str(
                (r.get("item_name") if isinstance(r, dict) else None)
                or (r.get("item_title") if isinstance(r, dict) else None)
                or (r.get("title") if isinstance(r, dict) else None)
                or ""
            ).strip()

            code_type = str(r.get("code_type") or "").strip().lower() if isinstance(r, dict) else ""
            if not name:
                if code_type == "ofd_platform":
                    name = "ОФД Платформа"
                elif code_type in ("ofd_first", "ofd_other"):
                    name = "ОФД"
                elif code_type == "edo":
                    name = "ЭДО"
                elif code_type == "service":
                    name = "Лицензия/прошивка"
                else:
                    name = "Код"
            lines.append(f"• {name}: {code_val}")
    else:
        ofd_codes = list(codes.get("ofd_first") or [])
        plat_codes = list(codes.get("ofd_platform") or [])
        ext_ofd = list(codes.get("ofd_other") or [])
        svc = list(codes.get("service") or [])
        edo = list(codes.get("edo") or [])

        if ofd_codes or plat_codes or ext_ofd or svc or edo:
            lines.append("")
            lines.append("Ваши коды:")
            for c in ofd_codes:
                lines.append(f"• ОФД: {c}")
            for c in ext_ofd:
                lines.append(f"• ОФД (внешний): {c}")
            for c in plat_codes:
                lines.append(f"• ОФД Платформа: {c}")
            for c in svc:
                lines.append(f"• Услуга: {c}")
            for c in edo:
                lines.append(f"• ЭДО: {c}")

        if (has_ofd_first or has_ofd_platform or has_ext_codes) and not (ofd_codes or plat_codes or ext_ofd or svc or edo):
            lines.append("")
            lines.append("Коды ОФД временно недоступны — заказ зафиксирован, мы передадим коды позже.")

    if has_equip:
        lines.append("")
        lines.append("Заказ на оборудование зафиксирован — мы свяжемся с вами для уточнения деталей.")
    if has_ext_serv_orders:
        lines.append("")
        lines.append("Заказы на коды зафиксированы — мы свяжемся с вами для уточнения деталей.")

    return "\n".join(lines)


# ── Клавиатура ожидания оплаты ───────────────────────────────────────

def _pending_payment_kb(order_id: str, pay_url: str | None,
                        show_retry: bool = False) -> InlineKeyboard:
    """Кнопки: [Оплатить] [Проверить оплату] [Создать новый платёж]."""
    rows = []
    if pay_url:
        rows.append([InlineButton("💳 Оплатить", url=pay_url)])

    check_cb = f"pay_check:{order_id}" if order_id else "pay_check:0"
    rows.append([InlineButton("✅ Проверить оплату", callback_data=check_cb)])

    if show_retry:
        rows.append([InlineButton("🔁 Создать новый платёж", callback_data="checkout")])

    return InlineKeyboard(rows)


# ── Checkout (оформление заказа) ─────────────────────────────────────

async def checkout_cb(ctx: MaxBotContext, cb: MaxCallback,
                      chat_id: str, internal_id: int) -> None:
    """
    callback_data = "checkout"

    Основной flow:
    1. Проверяем верификацию
    2. Проверяем stock
    3. Проверяем email
    4. Создаём заказ в БД
    5. Бронируем коды (_cart_to_order_items_reserved)
    6. init_payment через Tinkoff -> PaymentURL
    7. Показываем кнопку "Оплатить"
    """

    # 0) верификация
    if not await _require_verified(ctx, chat_id, internal_id):
        return

    cart = ctx.user_data.get("cart") or []
    if not cart:
        kb = InlineKeyboard([[InlineButton("⬅️ В магазин", callback_data="buy")]])
        await _send_or_edit(ctx, chat_id, "Корзина пуста.", keyboard=kb)
        return

    # 1) проверка наличия
    problems = await _validate_cart_stock(cart)
    if problems:
        lines = ["❌ Недостаточно товара в наличии.\n"]
        for p in problems:
            kind = p.get("kind")
            key = p.get("key")
            need = int(p.get("need") or 0)
            avail = int(p.get("available") or 0)
            if kind == "pool":
                title = "Наши коды: " + ("ОФД Платформа" if key == "ofd_platform" else "ОФД")
            else:
                title = "Внешний товар: " + str(key)
            lines.append(f"• {title}: нужно {need}, доступно {avail}")
        notice = "\n".join(lines) + "\n\nУменьшите количество в корзине и попробуйте снова."
        await _send_or_edit(
            ctx, chat_id,
            _format_cart_text(cart, notice=notice),
            keyboard=_kb_cart(cart),
        )
        return

    # 2) данные покупателя
    user = await get_user_by_internal_id(internal_id) or {}
    buyer_name = (user.get("first_name") or "").strip() or "Покупатель"
    buyer_phone = (user.get("phone") or "").strip() or None
    buyer_email = (user.get("email") or "").strip() or None

    # email обязателен для чека
    if not buyer_email:
        kb = InlineKeyboard([[InlineButton("⬅️ В корзину", callback_data="cart")]])
        mid = cb.message.message_id if cb.message else None
        await _send_or_edit(ctx, chat_id,
            "Для оплаты нужен email, чтобы вам пришёл чек.\n\nВведите email:",
            keyboard=kb, message_id=mid)
        _set_state(ctx, "co_email")
        return

    # 3) адрес доставки — только если в корзине есть оборудование
    _has_equip_in_cart = any((it.get("meta") or {}).get("equipment_id") is not None for it in cart)
    if _has_equip_in_cart and not ctx.user_data.get("_co_address_decided"):
        saved_address = (user.get("address") or "").strip() or None
        mid = cb.message.message_id if cb.message else None
        if saved_address:
            kb = InlineKeyboard([
                [InlineButton("✅ Да, использовать", callback_data="co_addr_use")],
                [InlineButton("✏️ Ввести другой", callback_data="co_addr_new")],
            ])
            await _send_or_edit(ctx, chat_id,
                f"Адрес доставки: {saved_address}\n\nИспользовать этот адрес?",
                keyboard=kb, message_id=mid)
        else:
            kb = InlineKeyboard([[InlineButton("⬅️ В корзину", callback_data="cart")]])
            await _send_or_edit(ctx, chat_id,
                "Введите адрес доставки:", keyboard=kb, message_id=mid)
        _set_state(ctx, "co_address")
        return

    # 4) итого
    total_rub = sum(int(x.get("price") or 0) * int(x.get("qty") or 1) for x in cart)

    # Предложение списания баллов (если ещё не ответил)
    if not ctx.user_data.get("_co_bonus_decided"):
        try:
            from services.checkout_bonus import get_bonus_checkout_info
            from services.bot_loyalty_ui import format_checkout_bonus_info
            bonus_info = await get_bonus_checkout_info(internal_id, total_rub)
            if bonus_info.get("available"):
                ctx.user_data["_co_bonus_info"] = bonus_info
                usable = bonus_info.get("usable_points", 0)
                discount = bonus_info.get("usable_rub_discount", 0)
                text = (
                    format_checkout_bonus_info(bonus_info, html=False)
                    + f"\n\nИтого к оплате: {total_rub} ₽"
                    + f"\nСо скидкой: {total_rub - discount:.0f} ₽"
                )
                kb = InlineKeyboard([
                    [InlineButton(f"✅ Списать {usable} балл. (−{discount:.0f} ₽)", callback_data="co_bonus_use")],
                    [InlineButton("❌ Не списывать", callback_data="co_bonus_skip")],
                ])
                mid = cb.message.message_id if cb.message else None
                await _send_or_edit(ctx, chat_id, text, keyboard=kb, message_id=mid)
                return
        except Exception:
            pass
        ctx.user_data["_co_bonus_decided"] = True

    # 5) создаём заказ в БД
    ctx.user_data.pop("_co_address_decided", None)
    user_id_str = str(internal_id)
    order_id_int = await create_order(
        user_id=user_id_str,
        amount=total_rub,
        buyer_name=buyer_name,
        buyer_phone=buyer_phone,
        tg_username=None,
        buyer_email=buyer_email,
        internal_user_id=internal_id,
        source="max",
        address=ctx.user_data.pop("_co_address", None),
    )
    if not order_id_int:
        kb = InlineKeyboard([[InlineButton("⬅️ В магазин", callback_data="buy")]])
        await _send_or_edit(ctx, chat_id, "⚠️ Не удалось создать заказ.", keyboard=kb)
        return
    order_id = str(order_id_int)
    tinkoff_order_id = build_tinkoff_order_id("order", order_id)

    # Резервируем бонусные баллы
    bonus_points = int(ctx.user_data.get("_co_bonus_points") or 0)
    bonus_discount = float(ctx.user_data.get("_co_bonus_discount") or 0)
    bonus_reservation_key = None
    if bonus_points > 0:
        try:
            from services.checkout_bonus import apply_bonus_discount
            res = await apply_bonus_discount(internal_id, order_id_int, bonus_points, total_rub)
            if res.get("ok"):
                bonus_reservation_key = res.get("reservation_key")
                bonus_discount = res.get("rub_discount", bonus_discount)
        except Exception:
            logger.exception("bonus reserve failed, proceeding without discount")
            bonus_points = 0
            bonus_discount = 0

    pay_rub = max(1, int(total_rub - bonus_discount)) if bonus_discount > 0 else total_rub

    # Очищаем checkout-bonus state
    for k in ("_co_bonus_decided", "_co_bonus_info", "_co_bonus_points", "_co_bonus_discount"):
        ctx.user_data.pop(k, None)

    # 5) бронируем коды
    try:
        order_items = await _cart_to_order_items_reserved(
            cart,
            user_id_str,
            tester_mode=_is_tester_max(ctx.user_data.get("_max_uid") or (cb.user.user_id if cb.user else None)),
        )
        await save_order_items(order_id, order_items)
    except Exception as e:
        logger.exception("reserve/save order_items failed: %s", e)
        await set_order_status(order_id, "CANCELLED", payment_info=f"reserve_failed: {e}")
        try:
            await rollback_order_reservations(order_id)
        except Exception:
            logger.exception("rollback_order_reservations failed")
        ctx.user_data["cart"] = []
        kb = InlineKeyboard([
            [InlineButton("⬅️ В магазин", callback_data="buy")],
            [InlineButton("🛒 Корзина", callback_data="cart")],
        ])
        await _send_or_edit(
            ctx, chat_id,
            "❌ Не удалось зарезервировать товар (скорее всего, он закончился).\n"
            "Пожалуйста, вернитесь в магазин и выберите другой вариант.",
            keyboard=kb,
        )
        return

    # meta — для совместимости
    cart_titles = [str(it.get("name") or it.get("title") or "Товар") for it in cart]
    if len(cart_titles) == 1:
        await set_order_item_meta(order_id, str(cart[0].get("sku") or "single"), cart_titles[0])
    else:
        await set_order_item_meta(order_id, "multi", " + ".join(cart_titles))

    if _is_tester_max(ctx.user_data.get("_max_uid") or (cb.user.user_id if cb.user else None)):
        fake_resp = {
            "Success": True,
            "Status": "CONFIRMED",
            "PaymentId": f"TEST_MAX_{order_id}",
            "Message": "tester_stub_auto_confirm",
            "Amount": int(pay_rub),
        }
        await set_order_payment_id(order_id, str(fake_resp["PaymentId"]))
        ctx.user_data["last_order_id"] = order_id
        ctx.user_data["last_payment_id"] = str(fake_resp["PaymentId"])
        ctx.user_data.pop("last_payment_url", None)

        await confirm_order_paid(order_id, user_id_str, payment_info=json.dumps(fake_resp, ensure_ascii=False))

        if bonus_reservation_key:
            try:
                from services.checkout_bonus import finalize_bonus_discount
                await finalize_bonus_discount(bonus_reservation_key)
            except Exception:
                logger.exception("finalize_bonus_discount failed for tester order %s", order_id)

        try:
            await process_external_ofd_after_payment(order_id, user_id_str)
        except Exception as e:
            logger.exception("process_external_ofd_after_payment failed for tester order %s: %s", order_id, e)

        codes = await issue_code_for_order(order_id, user_id_str)
        items = await get_order_items(order_id)
        order_row = await get_order(order_id) or {}
        detailed_codes = await get_order_codes_detailed(order_id)

        try:
            await push_sale_to_crm(
                order_id=order_id,
                total_rub=int(order_row.get("amount") or 0),
                cart=[],
                source="max",
                user_data={
                    "first_name": user.get("first_name", ""),
                    "last_name": user.get("last_name", ""),
                    "phone": user.get("phone", ""),
                    "email": user.get("email", ""),
                    "inn": user.get("inn", ""),
                    "city": user.get("city", ""),
                    "max_user_id": ctx.user_data.get("_max_uid", ""),
                },
                order_data={
                    "amount": order_row.get("amount"),
                    "status": order_row.get("status"),
                    "created_at": str(order_row.get("created_at")) if order_row.get("created_at") else None,
                    "buyer_name": order_row.get("buyer_name"),
                    "buyer_phone": order_row.get("buyer_phone"),
                    "buyer_email": order_row.get("buyer_email"),
                },
                items=items,
                codes=codes,
                detailed_codes=detailed_codes,
            )
        except Exception as e:
            logger.exception("push_sale_to_crm failed for tester order %s: %s", order_id, e)

        text = await _build_payment_success_text(
            order_row, items, codes,
            user_first_name=(ctx.user_data.get("first_name") or ""),
            detailed_codes=detailed_codes,
        )

        ctx.user_data["cart"] = []
        kb = InlineKeyboard([
            [InlineButton("🛍 В магазин", callback_data="buy")],
            [InlineButton("🔙 В меню", callback_data="back_to_main")],
        ])
        await _send_or_edit(
            ctx, chat_id,
            text + "\n\n🧪 Тестовая покупка подтверждена автоматически без банка.",
            keyboard=kb,
        )
        return

    # 6) Receipt + Tinkoff Init
    items_kop = _make_items_from_cart(cart)
    receipt = build_receipt(user, items_kop)

    try:
        resp = await _run_in_thread(
            init_payment,
            order_id=tinkoff_order_id,
            amount_rub=int(pay_rub),
            description=f"Заказ {order_id}",
            receipt=receipt,
        )
    except Exception as e:
        logger.exception("Tinkoff init_payment failed")
        await set_order_status(order_id, "CANCELLED", payment_info=f"tinkoff_init_failed: {e}")
        try:
            await rollback_order_reservations(order_id)
        except Exception:
            logger.exception("rollback_order_reservations failed")
        kb = InlineKeyboard([[InlineButton("⬅️ В магазин", callback_data="buy")]])
        await _send_or_edit(ctx, chat_id, f"⚠️ Не удалось создать платёж: {e}", keyboard=kb)
        return

    if not resp or resp.get("Success") is False:
        msg = resp.get("Message") or resp.get("Details") or str(resp)
        await set_order_status(order_id, "CANCELLED", payment_info=f"tinkoff_init_error: {msg}")
        try:
            await rollback_order_reservations(order_id)
        except Exception:
            logger.exception("rollback_order_reservations failed")
        kb = InlineKeyboard([[InlineButton("⬅️ В магазин", callback_data="buy")]])
        await _send_or_edit(ctx, chat_id, f"⚠️ Ошибка Tinkoff Init: {msg}", keyboard=kb)
        return

    payment_url = resp.get("PaymentURL") or resp.get("PaymentUrl") or ""
    payment_id = resp.get("PaymentId") or resp.get("PaymentID") or ""

    if not payment_url:
        await set_order_status(order_id, "CANCELLED", payment_info=f"tinkoff_no_url: {resp}")
        try:
            await rollback_order_reservations(order_id)
        except Exception:
            logger.exception("rollback_order_reservations failed")
        kb = InlineKeyboard([[InlineButton("⬅️ В магазин", callback_data="buy")]])
        await _send_or_edit(ctx, chat_id, f"⚠️ Платёж создан без ссылки: {resp}", keyboard=kb)
        return

    # сохраняем payment_id
    if payment_id:
        await set_order_payment_id(order_id, str(payment_id))

    ctx.user_data["last_order_id"] = order_id
    ctx.user_data["last_payment_id"] = str(payment_id) if payment_id else ""
    ctx.user_data["last_payment_url"] = payment_url

    # сохраняем резерв баллов для подтверждения/отмены
    if bonus_reservation_key:
        ctx.user_data["_co_bonus_reservation_key"] = bonus_reservation_key

    # 7) показываем кнопку "Оплатить"
    bonus_line = ""
    if bonus_discount > 0:
        bonus_line = f"Скидка баллами: −{bonus_discount:.0f} ₽ ({bonus_points} балл.)\n"
    text = (
        "Счёт сформирован ✅\n\n"
        f"Сумма: {total_rub} ₽\n"
        f"{bonus_line}"
        f"К оплате: {pay_rub} ₽\n"
        f"Заказ: {order_id}\n\n"
        "Нажмите «Оплатить», затем «Проверить оплату»."
    )
    await _send_or_edit(
        ctx, chat_id, text,
        keyboard=_pending_payment_kb(order_id, payment_url),
    )


# ── Проверка оплаты (ручная, по кнопке) ─────────────────────────────

async def check_payment_cb(ctx: MaxBotContext, cb: MaxCallback,
                           chat_id: str) -> None:
    """
    callback_data = "pay_check:{order_id}"

    Запрашивает статус оплаты через Tinkoff GetState.
    При CONFIRMED/AUTHORIZED — выдаём коды и показываем результат.
    """

    data = (cb.payload or "").strip()
    if data.startswith("pay_check:"):
        order_id = data.split("pay_check:", 1)[1].strip()
    else:
        order_id = ctx.user_data.get("last_order_id") or ""

    if not order_id:
        kb = InlineKeyboard([[InlineButton("⬅️ В магазин", callback_data="buy")]])
        await _send_or_edit(ctx, chat_id, "Не нашёл заказ для проверки оплаты.", keyboard=kb)
        return

    order = await get_order(order_id) or {}
    payment_id = (
        order.get("payment_id")
        or ctx.user_data.get("last_payment_id")
        or ""
    ).strip()

    if not payment_id:
        kb = InlineKeyboard([[InlineButton("🔁 Создать платёж", callback_data="checkout")]])
        await _send_or_edit(ctx, chat_id, "У этого заказа нет payment_id. Создайте платёж заново.", keyboard=kb)
        return

    # GetState через Tinkoff
    try:
        resp = await _run_in_thread(get_payment_state, payment_id=str(payment_id))
    except Exception as e:
        logger.exception("Tinkoff get_payment_state failed")
        pay_url = ctx.user_data.get("last_payment_url")
        await _send_or_edit(
            ctx, chat_id,
            f"⚠️ Не удалось проверить оплату: {e}",
            keyboard=_pending_payment_kb(order_id, pay_url, show_retry=True),
        )
        return

    if not resp or resp.get("Success") is False:
        msg = resp.get("Message") or resp.get("Details") or str(resp)
        pay_url = ctx.user_data.get("last_payment_url")
        await _send_or_edit(
            ctx, chat_id,
            f"⚠️ Ошибка Tinkoff GetState: {msg}",
            keyboard=_pending_payment_kb(order_id, pay_url, show_retry=True),
        )
        return

    status = str(resp.get("Status") or "").upper()

    # ещё не оплачено
    if status not in ("CONFIRMED", "AUTHORIZED"):
        pay_url = ctx.user_data.get("last_payment_url")
        await _send_or_edit(
            ctx, chat_id,
            f"Оплата пока не подтверждена.\nСтатус: {status or '—'}\n\n"
            "Если вы уже оплатили — подождите 10–30 секунд и нажмите «Проверить оплату» снова.",
            keyboard=_pending_payment_kb(order_id, pay_url, show_retry=True),
        )
        return

    # ── Оплата подтверждена ──
    internal_id = ctx.user_data.get("_db_id")
    user_id_str = str(internal_id) if internal_id else ""

    await confirm_order_paid(order_id, user_id_str, payment_info=json.dumps(resp, ensure_ascii=False))

    # Финализация бонусной скидки
    _rk = ctx.user_data.pop("_co_bonus_reservation_key", "")
    if _rk:
        try:
            from services.checkout_bonus import finalize_bonus_discount
            await finalize_bonus_discount(_rk)
        except Exception:
            logger.exception("finalize_bonus_discount failed for order %s", order_id)

    try:
        await process_external_ofd_after_payment(order_id, user_id_str)
    except Exception as e:
        logger.exception("process_external_ofd_after_payment failed: %s", e)

    codes = await issue_code_for_order(order_id, user_id_str)
    items = await get_order_items(order_id)
    order_row = await get_order(order_id) or order
    detailed_codes = await get_order_codes_detailed(order_id)

    # Push sale notification to Django CRM
    try:
        await push_sale_to_crm(
            order_id=order_id,
            total_rub=int(order_row.get("amount") or 0),
            cart=[],
            source="max",
            user_data={
                "first_name": ctx.user_data.get("first_name", ""),
                "last_name": ctx.user_data.get("last_name", ""),
                "phone": ctx.user_data.get("phone", ""),
                "email": ctx.user_data.get("email", ""),
                "inn": ctx.user_data.get("inn", ""),
                "city": ctx.user_data.get("city", ""),
                "max_user_id": ctx.user_data.get("_max_uid", ""),
            },
            order_data={
                "amount": order_row.get("amount"),
                "status": order_row.get("status"),
                "created_at": str(order_row.get("created_at")) if order_row.get("created_at") else None,
                "buyer_name": order_row.get("buyer_name"),
                "buyer_phone": order_row.get("buyer_phone"),
                "buyer_email": order_row.get("buyer_email"),
            },
            items=items,
            codes=codes,
            detailed_codes=detailed_codes,
        )
    except Exception as e:
        logger.exception("push_sale_to_crm failed: %s", e)

    text = await _build_payment_success_text(
        order_row, items, codes,
        user_first_name=(ctx.user_data.get("first_name") or ""),
        detailed_codes=detailed_codes,
    )

    # чистим корзину
    ctx.user_data["cart"] = []

    kb = InlineKeyboard([
        [InlineButton("🛍 В магазин", callback_data="buy")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    mid = await _send_or_edit(ctx, chat_id, text, keyboard=kb)
    # Payment success — удаляем через 5 минут
    if mid:
        asyncio.create_task(_max_ui._schedule_delete_max(ctx, chat_id, mid, ttl_sec=300))


# ── Обработка ввода адреса при checkout ──────────────────────────────

async def checkout_address_input(ctx: MaxBotContext, msg: MaxMessage,
                                 chat_id: str, internal_id: int) -> bool:
    """Обрабатывает ввод адреса в состоянии co_address.
    Возвращает True если обработал.
    """
    state = ctx.user_data.get("_state") or ""
    if state != "co_address":
        return False

    address = (msg.text or "").strip()
    if not address:
        await _max_ui.render_notice(ctx, chat_id, "Введите адрес доставки:")
        return True

    from db.users import set_user_address
    await set_user_address(internal_user_id=internal_id, address=address)

    ctx.user_data["_co_address"] = address
    ctx.user_data["_co_address_decided"] = True
    _set_state(ctx, ST_IDLE)

    fake_cb = MaxCallback(callback_id="", payload="checkout")
    await checkout_cb(ctx, fake_cb, chat_id, internal_id)
    return True


# ── Обработка ввода email при checkout ───────────────────────────────

async def checkout_email_input(ctx: MaxBotContext, msg: MaxMessage,
                               chat_id: str, internal_id: int) -> bool:
    """Обрабатывает ввод email в состоянии co_email.
    Возвращает True если обработал (состояние было co_email).
    """
    state = ctx.user_data.get("_state") or ""
    if state != "co_email":
        return False

    email = (msg.text or "").strip()
    if not email or "@" not in email:
        await _max_ui.render_notice(ctx, chat_id, "Некорректный email. Попробуйте ещё раз:")
        return True

    # сохраняем email в БД
    from db import update_user_by_internal_id
    await update_user_by_internal_id(internal_id, email=email)

    _set_state(ctx, ST_IDLE)

    # повторяем checkout (теперь email есть)
    fake_cb = MaxCallback(
        callback_id="",
        payload="checkout",
    )
    await checkout_cb(ctx, fake_cb, chat_id, internal_id)
    return True


# ══════════════════════════════════════════════════════════════════════
#  АВТОПРОВЕРКА ОПЛАТЫ (аналог auto_check_payment_job из TG бота)
# ══════════════════════════════════════════════════════════════════════

async def auto_check_payment_job(bot_instance) -> None:
    """
    Глобальная фоновая задача: каждые 30 сек проверяет все заказы
    со статусом INIT + payment_id через Tinkoff GetState.
    При CONFIRMED/AUTHORIZED — подтверждает оплату, выдаёт коды,
    шлёт сообщение пользователю.
    Через PAYMENT_WAIT_TIMEOUT_SEC — таймаут, отмена заказа.
    """
    api = bot_instance.api
    try:
        orders = await list_orders_awaiting_payment(max_age_minutes=PAYMENT_WAIT_LOOKBACK_MIN)
    except Exception:
        logger.exception("auto_check_payment_job: list_orders_awaiting_payment failed")
        return

    for order in orders:
        order_id = str(order.get("id") or order.get("order_id") or "")
        payment_id = str(order.get("payment_id") or "").strip()
        user_id = order.get("user_id")  # internal id
        created_at = order.get("created_at")

        if not order_id or not payment_id:
            continue

        # таймаут PAYMENT_WAIT_TIMEOUT_SEC с момента создания
        if created_at:
            try:
                if isinstance(created_at, str):
                    created_at = _dt.datetime.fromisoformat(created_at)
                age_sec = (
                    _dt.datetime.now(_dt.timezone.utc) - created_at.replace(tzinfo=_dt.timezone.utc)
                ).total_seconds()
                if age_sec > PAYMENT_WAIT_TIMEOUT_SEC:
                    try:
                        await set_order_status(order_id, "CANCELLED", payment_info="payment_timeout")
                        await rollback_order_reservations(order_id)
                    except Exception:
                        logger.exception("auto_check: timeout cancel/rollback failed for order %s", order_id)
                    continue
            except Exception:
                pass

        # проверяем статус через Tinkoff
        try:
            resp = await _run_in_thread(get_payment_state, payment_id=payment_id)
        except Exception:
            logger.exception("auto_check: get_payment_state failed for order %s", order_id)
            continue

        if not resp or resp.get("Success") is False:
            continue

        status = str(resp.get("Status") or "").upper()
        if status not in ("CONFIRMED", "AUTHORIZED"):
            continue

        # ── Оплата подтверждена ──
        user_id_str = str(user_id) if user_id else ""
        await confirm_order_paid(order_id, user_id_str,
                                 payment_info=json.dumps(resp, ensure_ascii=False))

        # Финализация бонусной скидки (auto-check): ctx недоступен в фоновом задании
        _rk = ""
        if _rk:
            try:
                from services.checkout_bonus import finalize_bonus_discount
                await finalize_bonus_discount(_rk)
            except Exception:
                logger.exception("auto_check: finalize_bonus_discount failed for order %s", order_id)

        try:
            await process_external_ofd_after_payment(order_id, user_id_str)
        except Exception as e:
            logger.exception("auto_check: process_external_ofd_after_payment failed: %s", e)

        codes = await issue_code_for_order(order_id, user_id_str)
        items = await get_order_items(order_id)
        order_row = await get_order(order_id) or order
        detailed_codes = await get_order_codes_detailed(order_id)

        # определяем first_name для текста
        first_name = ""
        if user_id:
            try:
                u = await get_user_by_internal_id(int(user_id))
                first_name = (u or {}).get("first_name") or ""
            except Exception:
                pass

        # Push sale notification to Django CRM
        try:
            user_data = {}
            if user_id:
                try:
                    u = await get_user_by_internal_id(int(user_id))
                    if u:
                        user_data = {
                            "first_name": u.get("first_name", ""),
                            "last_name": u.get("last_name", ""),
                            "phone": u.get("phone", ""),
                            "email": u.get("email", ""),
                            "inn": u.get("inn", ""),
                            "city": u.get("city", ""),
                            "max_user_id": u.get("max_user_id", ""),
                        }
                except Exception:
                    pass
            
            await push_sale_to_crm(
                order_id=order_id,
                total_rub=int(order_row.get("amount") or 0),
                cart=[],
                source="max",
                user_data=user_data,
                order_data={
                    "amount": order_row.get("amount"),
                    "status": order_row.get("status"),
                    "created_at": str(order_row.get("created_at")) if order_row.get("created_at") else None,
                    "buyer_name": order_row.get("buyer_name"),
                    "buyer_phone": order_row.get("buyer_phone"),
                    "buyer_email": order_row.get("buyer_email"),
                },
                items=items,
                codes=codes,
                detailed_codes=detailed_codes,
            )
        except Exception as e:
            logger.exception("auto_check: push_sale_to_crm failed: %s", e)

        text = await _build_payment_success_text(
            order_row, items, codes,
            user_first_name=first_name,
            detailed_codes=detailed_codes,
        )

        kb = InlineKeyboard([
            [InlineButton("🛍 В магазин", callback_data="buy")],
            [InlineButton("🔙 В меню", callback_data="back_to_main")],
        ])

        # отправляем уведомление пользователю
        # нужен max_user_id чтобы отправить в MAX
        try:
            u = await get_user_by_internal_id(int(user_id)) if user_id else None
            max_uid = (u or {}).get("max_user_id")
            if max_uid:
                # Удаляем сообщение с кнопками "Оплатить" / "Проверить оплату"
                from message_cleanup import max_safe_delete_message
                from message_cleanup import MAX_LAST_MENU_KEY
                try:
                    ud = bot_instance._user_data.get(int(max_uid), {})
                    pay_screen_mid = (ud or {}).get(MAX_LAST_MENU_KEY)
                    if pay_screen_mid:
                        await max_safe_delete_message(api, str(pay_screen_mid))
                        ud.pop(MAX_LAST_MENU_KEY, None)
                except Exception:
                    logger.exception("auto_check: failed to delete payment screen for max_uid=%s", max_uid)

                await api.send_message_to_user(
                    user_id=int(max_uid),
                    text=text,
                    keyboard=kb,
                )
                logger.info("auto_check: payment confirmed for order %s, notified max_user %s",
                            order_id, max_uid)
            else:
                logger.warning("auto_check: order %s confirmed but no max_user_id for user %s",
                               order_id, user_id)
        except Exception:
            logger.exception("auto_check: failed to notify user for order %s", order_id)


# ══════════════════════════════════════════════════════════════════════
#  АДМИНКА (аналог admin_menu, admin_stats и т.д. из TG бота)
# ══════════════════════════════════════════════════════════════════════

# Админы MAX — читаем из MAX_ADMIN_IDS (max user_id, через запятую)
MAX_ADMIN_IDS: set[int] = set()
for _x in os.getenv("MAX_ADMIN_IDS", "").replace(" ", "").split(","):
    if _x:
        try:
            MAX_ADMIN_IDS.add(int(_x))
        except ValueError:
            pass


def _is_max_admin(max_user_id: int) -> bool:
    return max_user_id in MAX_ADMIN_IDS


async def admin_menu_cb(ctx: MaxBotContext, cb: MaxCallback,
                        chat_id: str) -> None:
    """callback_data = 'admin_menu' — показать админ-панель."""

    max_uid = cb.user.user_id if cb.user else 0
    if not _is_max_admin(max_uid):
        return

    kb = InlineKeyboard([
        [InlineButton("📊 Статистика", callback_data="admin_stats")],
        [InlineButton("🧾 Последние выдачи", callback_data="admin_recent")],
        [InlineButton("🧪 Выдать код БЕЗ оплаты", callback_data="admin_give_code")],
        [InlineButton("🔔 Рассылка напоминаний (тест)", callback_data="admin_ofd_broadcast_all")],
        [InlineButton("⬅️ В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, "🛠 Админка", keyboard=kb)


async def admin_stats_cb(ctx: MaxBotContext, cb: MaxCallback,
                         chat_id: str) -> None:
    """callback_data = 'admin_stats'"""

    max_uid = cb.user.user_id if cb.user else 0
    if not _is_max_admin(max_uid):
        return

    codes_stat = await get_stats()
    orders_stat = await get_orders_stats()
    text = (
        "📊 Статистика\n\n"
        f"Коды: всего {codes_stat['total']}, свободно {codes_stat['free']}, выдано {codes_stat['used']}\n"
        f"Заказы: всего {orders_stat['total']}, подтверждено {orders_stat['confirmed']}, "
        f"в работе {orders_stat['init']}, прочее {orders_stat['other']}"
    )
    kb = InlineKeyboard([[InlineButton("⬅️ Назад", callback_data="admin_menu")]])
    await _send_or_edit(ctx, chat_id, text, keyboard=kb)


async def admin_recent_cb(ctx: MaxBotContext, cb: MaxCallback,
                          chat_id: str) -> None:
    """callback_data = 'admin_recent'"""

    max_uid = cb.user.user_id if cb.user else 0
    if not _is_max_admin(max_uid):
        return

    rows = await get_recent_issues(limit=20)
    if not rows:
        text = "Пока нет выданных кодов."
    else:
        lines = []
        for r in rows:
            name = r.get("buyer_name") or "—"
            phone = r.get("buyer_phone") or "—"
            lines.append(
                f"• {r.get('code', '—')} — {name} ({phone}) — "
                f"user={r.get('user_id', '—')} — {r.get('issued_at', '—')} — order={r.get('order_id', '—')}"
            )
        text = "🧾 Последние выдачи\n\n" + "\n".join(lines)

    # обрезаем, если слишком длинное
    if len(text) > 3500:
        text = text[:3500] + "\n…"

    kb = InlineKeyboard([[InlineButton("⬅️ Назад", callback_data="admin_menu")]])
    await _send_or_edit(ctx, chat_id, text, keyboard=kb)


async def admin_give_code_cb(ctx: MaxBotContext, cb: MaxCallback,
                             chat_id: str) -> None:
    """callback_data = 'admin_give_code' — выдать код без оплаты."""

    max_uid = cb.user.user_id if cb.user else 0
    if not _is_max_admin(max_uid):
        return

    internal_id = ctx.user_data.get("_db_id") or 0
    uid_str = str(internal_id)

    order_id_int = await create_order(
        user_id=uid_str,
        amount=0,
        buyer_name="ADMIN TEST",
        internal_user_id=internal_id,
        source="max",
    )
    if not order_id_int:
        kb = InlineKeyboard([[InlineButton("⬅️ Назад", callback_data="admin_menu")]])
        await _send_or_edit(ctx, chat_id, "⚠️ Не удалось создать заказ", keyboard=kb)
        return

    order_id = str(order_id_int)

    code_id = await reserve_pool_code_for_user(uid_str, "ofd_first", sell_price=0, months_number=12)
    if not code_id:
        kb = InlineKeyboard([[InlineButton("⬅️ Назад", callback_data="admin_menu")]])
        await _send_or_edit(ctx, chat_id, "❌ Нет свободных кодов в пуле ОФД.", keyboard=kb)
        return

    await save_order_items(order_id, [{
        "code_id": int(code_id),
        "qty": 1,
        "price": 0,
    }])

    await confirm_order_paid(order_id, uid_str, payment_info="admin_manual_issue")

    codes = await issue_code_for_order(order_id=order_id, user_id=uid_str)
    ofd_codes = codes.get("ofd_first") or []

    if not ofd_codes:
        kb = InlineKeyboard([[InlineButton("⬅️ Назад", callback_data="admin_menu")]])
        await _send_or_edit(ctx, chat_id, "😔 Свободных кодов ОФД нет.", keyboard=kb)
        return

    kb = InlineKeyboard([[InlineButton("⬅️ Назад", callback_data="admin_menu")]])
    await _send_or_edit(ctx, chat_id, f"🔑 Код ОФД выдан: {ofd_codes[0]}", keyboard=kb)


async def admin_ofd_broadcast_all_cb(ctx: MaxBotContext, cb: MaxCallback,
                                     chat_id: str) -> None:
    """callback_data = 'admin_ofd_broadcast_all' — тестовая рассылка."""

    max_uid = cb.user.user_id if cb.user else 0
    if not _is_max_admin(max_uid):
        return

    kb_back = InlineKeyboard([[InlineButton("⬅️ Назад", callback_data="admin_menu")]])
    await _send_or_edit(ctx, chat_id, "⏱ Запускаю тестовую рассылку всем с ОФД-слотами…", keyboard=kb_back)

    sent, skipped, errors = await _ofd_reminder_broadcast_all_max(ctx.api)

    text = (
        f"Готово.\n"
        f"✅ Отправлено: {sent}\n"
        f"🚫 Без max_user_id: {skipped}\n"
        f"⚠️ Ошибок: {errors}"
    )
    await _send_or_edit(ctx, chat_id, text, keyboard=kb_back)


async def _ofd_reminder_broadcast_all_max(api) -> tuple[int, int, int]:
    """Тестовая рассылка для MAX: шлём всем, у кого есть max_user_id и ОФД-слоты."""
    today = _dt.date.today()
    sent = 0
    skipped_no_user = 0
    errors = 0

    for client in await get_all_clients_with_ofd_max():
        max_uid = client.get("max_user_id")
        if not max_uid:
            skipped_no_user += 1
            continue
        inn = client.get("inn")
        slots = client.get("ofd_slots") or []
        for s in slots:
            exp_str = s.get("ofd_expires_at")
            days_left = None
            date_obj = None
            try:
                date_obj = _dt.date.fromisoformat(exp_str)
                days_left = (date_obj - today).days
            except Exception:
                pass

            text_lines = [
                "🧪 Тест напоминания (проверка доставки)",
                f"ККТ: {s.get('kkt_slot')}",
                f"ИНН: {inn}",
            ]
            if date_obj:
                text_lines.append(f"Дата окончания: {date_obj:%d.%m.%Y}")
            else:
                text_lines.append(f"Дата окончания: {exp_str or '—'}")
            if days_left is not None:
                text_lines.append(f"До конца: {days_left} дн.")

            try:
                await api.send_message_to_user(
                    user_id=int(max_uid),
                    text="\n".join(text_lines),
                )
                sent += 1
            except Exception as e:
                errors += 1
                logger.warning("[ofd_broadcast_all_max] failed to notify %s: %s", max_uid, e)

            await asyncio.sleep(0.04)

    logger.info("[ofd_broadcast_all_max] sent=%d, skipped_no_user=%d, errors=%d",
                sent, skipped_no_user, errors)
    return sent, skipped_no_user, errors


# ══════════════════════════════════════════════════════════════════════
#  ДОП. ВЕРИФИКАЦИЯ (аналог extra_verify_start из TG бота)
# ══════════════════════════════════════════════════════════════════════

async def extra_verify_cb(ctx: MaxBotContext, cb: MaxCallback,
                          chat_id: str) -> None:
    """callback_data = 'extra_verify' — отправить данные на доп. верификацию."""

    if ctx.user_data.get("extra_verify_done"):
        await _send_or_edit(
            ctx, chat_id,
            "Вы уже отправили заявку на дополнительную верификацию. "
            "Мы всё проверим и свяжемся с вами.",
            keyboard=InlineKeyboard([[InlineButton("🔙 В меню", callback_data="back_to_main")]]),
        )
        return

    ud = ctx.user_data
    missing = []
    if not (ud.get("first_name") or "").strip():
        missing.append("имя")
    if not (ud.get("last_name") or "").strip():
        missing.append("фамилия")
    if not (ud.get("phone") or "").strip():
        missing.append("телефон")
    if not (ud.get("inn") or "").strip():
        missing.append("ИНН")
    if not (ud.get("email") or "").strip():
        missing.append("e-mail")
    if not (ud.get("city") or "").strip():
        missing.append("город")

    if missing:
        text = (
            "Чтобы отправить данные на дополнительную верификацию, нужно заполнить профиль:\n\n"
            + "\n".join(f"• {m}" for m in missing)
            + "\n\nОткройте «👤 Мой профиль» → «✏️ Редактировать профиль» и заполните недостающие поля.\n"
              "После этого снова нажмите «Дополнительная верификация»."
        )
        await _send_or_edit(
            ctx, chat_id, text,
            keyboard=InlineKeyboard([[InlineButton("🔙 В меню", callback_data="back_to_main")]]),
        )
        return

    # все данные есть — шлём в Django CRM
    try:
        await push_verification_to_crm(
            user_data={
                "first_name": ud.get("first_name", ""),
                "last_name": ud.get("last_name", ""),
                "phone": ud.get("phone", ""),
                "email": ud.get("email", ""),
                "inn": ud.get("inn", ""),
                "city": ud.get("city", ""),
                "max_user_id": ud.get("_max_uid", ""),
            },
            source="max",
        )
    except Exception as e:
        logger.exception("push_verification_to_crm failed: %s", e)
        await _send_or_edit(
            ctx, chat_id,
            f"Не удалось отправить данные в CRM: {e}",
            keyboard=InlineKeyboard([[InlineButton("🔙 В меню", callback_data="back_to_main")]]),
        )
        return

    ctx.user_data["extra_verify_done"] = True
    ctx.user_data["extra_verify_offer"] = False

    await _send_or_edit(
        ctx, chat_id,
        "✅ Заявка на дополнительную верификацию отправлена.\n"
        "Мы проверим данные и при необходимости свяжемся с вами.",
        keyboard=InlineKeyboard([[InlineButton("🔙 В меню", callback_data="back_to_main")]]),
    )


# ══════════════════════════════════════════════════════════════════════
#  ПОДЗАДАЧА 7: Регистрация всех обработчиков каталога
# ══════════════════════════════════════════════════════════════════════

def _cb_chat_id(cb: MaxCallback, ctx: MaxBotContext) -> str:
    """Извлечь chat_id из callback."""
    raw = cb.message.chat_id if cb.message else ""
    return _resolve_chat_id(ctx, raw)


def _cb_internal_id(ctx: MaxBotContext) -> int:
    """Извлечь internal_id (users.id) из user_data."""
    return ctx.user_data.get("_db_id") or 0


def register_catalog_handlers(bot) -> None:
    """Подключить все обработчики каталога/магазина к MaxBot.

    Каждый callback handler в MaxBot принимает (ctx, cb).
    Функции каталога принимают (ctx, cb, chat_id) или (ctx, cb, chat_id, internal_id).
    Обёртки извлекают chat_id и internal_id из ctx/cb.
    """

    # ── Магазин: главное меню ─────────────────────────────────────
    async def _buy(ctx, cb):
        chat_id = _cb_chat_id(cb, ctx)
        await show_shop(ctx, chat_id, cb=cb)
    bot.register_callback("buy", _buy)

    async def _shop_consult(ctx, cb):
        await shop_consult_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("shop_consult:*", _shop_consult)

    # ── Оборудование ─────────────────────────────────────────────
    async def _shop_equip(ctx, cb):
        await show_shop_equipment(ctx, _cb_chat_id(cb, ctx), cb=cb)
    bot.register_callback("shop_equipment", _shop_equip)
    async def _shop_paper(ctx, cb):
        await show_shop_paper(ctx, _cb_chat_id(cb, ctx), cb=cb)
    bot.register_callback("shop_paper", _shop_paper)

    async def _eq_root(ctx, cb):
        await eq_root_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("eq_root:*", _eq_root)

    async def _eq_cat(ctx, cb):
        await eq_category_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("eq_cat:*", _eq_cat)

    async def _eq_view(ctx, cb):
        await eq_view_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("eq_view:*", _eq_view)

    async def _eq_add(ctx, cb):
        await eq_add_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("eq_add:*", _eq_add)

    # ── Внешние товары: ОФД ──────────────────────────────────────
    async def _svc_ofd(ctx, cb):
        await show_services_ofd(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("services_ofd", _svc_ofd)

    async def _ext_ofd_p(ctx, cb):
        await ext_ofd_provider_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("ext_ofd_p:*", _ext_ofd_p)

    async def _ext_ofd_add(ctx, cb):
        await ext_ofd_add_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("ext_ofd_add:*", _ext_ofd_add)

    # ── Внешние товары: Сервис (лицензии) ────────────────────────
    async def _svc_service(ctx, cb):
        await show_services_service(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("services_service", _svc_service)

    async def _ext_srv_p(ctx, cb):
        await ext_srv_provider_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("ext_srv_p:*", _ext_srv_p)

    async def _ext_srv_add(ctx, cb):
        await ext_srv_add_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("ext_srv_add:*", _ext_srv_add)

    # ── Внешние товары: Сервис-заказы (прошивки) ─────────────────
    async def _ext_srv_ord_p(ctx, cb):
        await ext_srv_ord_provider_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("ext_srv_ord_p:*", _ext_srv_ord_p)

    async def _ext_srv_ord_add(ctx, cb):
        await ext_srv_ord_add_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("ext_srv_ord_add:*", _ext_srv_ord_add)

    # ── Внешние товары: ЭДО ──────────────────────────────────────
    async def _svc_edo(ctx, cb):
        await show_services_edo(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("services_edo", _svc_edo)

    async def _ext_edo_p(ctx, cb):
        await ext_edo_provider_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("ext_edo_p:*", _ext_edo_p)

    async def _ext_edo_add(ctx, cb):
        await ext_edo_add_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("ext_edo_add:*", _ext_edo_add)

    # ── Встроенные товары (product_ofd_first, product_ofd_platform) ──
    async def _product(ctx, cb):
        await add_builtin_product_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("product_*", _product)

    # ── Корзина ──────────────────────────────────────────────────
    async def _cart(ctx, cb):
        await show_cart(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("cart", _cart)

    async def _cart_inc(ctx, cb):
        await cart_inc_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("cart_inc:*", _cart_inc)

    async def _cart_dec(ctx, cb):
        await cart_dec_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("cart_dec:*", _cart_dec)

    async def _del(ctx, cb):
        await remove_from_cart_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("del_*", _del)

    async def _clear_cart(ctx, cb):
        await clear_cart_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("clear_cart", _clear_cart)

    async def _cart_noop(ctx, cb):
        await cart_noop_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("cart_noop", _cart_noop)

    # ── Checkout + оплата ────────────────────────────────────────
    async def _checkout(ctx, cb):
        await checkout_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("checkout", _checkout)

    async def _co_addr_use(ctx, cb):
        user = await get_user_by_internal_id(_cb_internal_id(ctx)) or {}
        saved = (user.get("address") or "").strip()
        ctx.user_data["_co_address"] = saved or None
        ctx.user_data["_co_address_decided"] = True
        _set_state(ctx, ST_IDLE)
        await checkout_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("co_addr_use", _co_addr_use)

    async def _co_addr_new(ctx, cb):
        kb = InlineKeyboard([[InlineButton("⬅️ В корзину", callback_data="cart")]])
        await _max_ui.render_screen(ctx, _cb_chat_id(cb, ctx),
            "Введите новый адрес доставки:", reply_markup=kb)
        _set_state(ctx, "co_address")
    bot.register_callback("co_addr_new", _co_addr_new)

    async def _co_bonus_use(ctx, cb):
        info = ctx.user_data.get("_co_bonus_info") or {}
        ctx.user_data["_co_bonus_decided"] = True
        ctx.user_data["_co_bonus_points"] = info.get("usable_points", 0)
        ctx.user_data["_co_bonus_discount"] = info.get("usable_rub_discount", 0)
        await checkout_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("co_bonus_use", _co_bonus_use)

    async def _co_bonus_skip(ctx, cb):
        ctx.user_data["_co_bonus_decided"] = True
        ctx.user_data["_co_bonus_points"] = 0
        ctx.user_data["_co_bonus_discount"] = 0
        await checkout_cb(ctx, cb, _cb_chat_id(cb, ctx), _cb_internal_id(ctx))
    bot.register_callback("co_bonus_skip", _co_bonus_skip)

    async def _pay_check(ctx, cb):
        await check_payment_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("pay_check:*", _pay_check)

    # ── Админка ──────────────────────────────────────────────────
    async def _admin_menu(ctx, cb):
        await admin_menu_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("admin_menu", _admin_menu)

    async def _admin_stats(ctx, cb):
        await admin_stats_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("admin_stats", _admin_stats)

    async def _admin_recent(ctx, cb):
        await admin_recent_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("admin_recent", _admin_recent)

    async def _admin_give_code(ctx, cb):
        await admin_give_code_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("admin_give_code", _admin_give_code)

    async def _admin_broadcast(ctx, cb):
        await admin_ofd_broadcast_all_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("admin_ofd_broadcast_all", _admin_broadcast)

    # ── Доп. верификация ─────────────────────────────────────────
    async def _extra_verify(ctx, cb):
        await extra_verify_cb(ctx, cb, _cb_chat_id(cb, ctx))
    bot.register_callback("extra_verify", _extra_verify)

    logger.info("MAX-бот: обработчики каталога/магазина подключены")
