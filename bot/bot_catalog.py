# bot_catalog.py
"""
Логика каталога, корзины, оплаты и админки.
"""

from typing import Optional, Dict, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from tinkoff import init_payment, get_payment_state, build_tinkoff_order_id

import os
import sys

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
from db import (
    upsert_user,
    get_user,

    create_order,
    get_order,
    set_order_payment_id,
    set_order_status,
    set_order_item_meta,
    confirm_order_paid,

    save_order_items,
    get_order_items,
    issue_code_for_order,

    add_codes_bulk,
    get_stats,
    get_orders_stats,
    get_recent_issues,

    get_free_codes_count,
    reserve_pool_code_for_user,
    reserve_external_code_for_user,
    rollback_order_reservations,
    get_order_reserved_external_codes,

    get_all_clients_with_ofd,
    get_equipment_categories, get_equipment_items, get_equipment_by_id, get_equipment_available_count, get_equipment_id_by_name_category,
    get_ext_products_all,
    save_external_code_for_order_item,
    get_order_codes_detailed,
    list_directors,
    get_nav_snapshot,
    get_system_setting_number
)

import os
import time
import json
import asyncio
import logging
import datetime

from telegram.error import BadRequest

from bot_common import (
    is_tester,
    STATE_IDLE,
    STATE_CONSENT,
    STATE_PRODUCT_SELECT,
    STATE_CART,
    STATE_CO_EMAIL,
    STATE_CO_INN,
    STATE_CO_CITY,
    STATE_ADMIN_ADD_CODES,
    STATE_EDIT_MENU,
    STATE_EDIT_FIRSTNAME,
    STATE_EDIT_LASTNAME,
    STATE_EDIT_PHONE,
    STATE_EDIT_EMAIL,
    STATE_EDIT_INN,
    STATE_EDIT_CITY,
    S0_START,
    S0_GUEST,
    S1_IDENT,
    S2_WAIT_CONTACT,
    S4_WAIT_INN,
    S5_HOME,
    STATE_FORM_TP,
    STATE_FORM_ADDR,
    STATE_FORM_ACT,
    STATE_FORM_ASSORT,
    STATE_FORM_SERVICES,
    STATE_FORM_SNO,
    STATE_FORM_MARKED,
    STATE_FORM_FNTERM,
    STATE_FORM_OFD,
    STATE_FORM_BANKS,
    STATE_CO_KKT_FACTORY_NUMBER,
    STATE_CO_BONUS,
    STATE_CO_ADDRESS,
    _debounce_cb,
    send_or_edit,
    nav_kb,
    show_main_menu,
    is_admin,
)
import time
import math
from extern_ofd import get_products, reserve_ofd_order, pay_reserved_ofd_order, create_ofd_order
from django_crm import push_sale_to_crm

logger = logging.getLogger(__name__)

import concurrent.futures

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

async def _run_in_thread(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


async def _require_verified_for_commerce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Доступ к корзине/покупкам только для верифицированных пользователей.
    """
    try:
        uid = int(update.effective_user.id)
    except Exception:
        return False

    snap = await get_nav_snapshot(
        uid,
        tg_username=context.user_data.get("tg_username"),
    )
    if bool(snap.get("verified")):
        return True

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
    ])
    await send_or_edit(
        update,
        "⛔ Доступ к корзине и покупкам открыт только после верификации.\n\n"
        "Подтвердите телефон и укажите ИНН.",
        kb,
        context=context,
    )
    return False


# ===== Каталог / корзина =====
# SKU каталога услуг
def _env_price(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception as e:
        print(e)
        return default


PAYMENT_WAIT_TIMEOUT_SEC = _env_price("PAYMENT_WAIT_TIMEOUT_SEC", 30 * 60)

PRODUCTS = {
    "ofd_first": {"name": "Первый ОФД (12 месяцев)", "price": _env_price("PRICE_OFD_FIRST", 2500)},
    "ofd_platform": {"name": "Платформа ОФД (12 месяцев)", "price": _env_price("PRICE_OFD_PLATFORM", 2500)},
}
EXT_OFD_CACHE = {
    "products": None,
    "updated_at": 0
}

EXT_OFD_TTL = 60 * 60 * 2  # 2 часа

EXT_SERVICE_CACHE = {
    "products": None,
    "updated_at": 0
}
EXT_SERVICE_ORDERS_CACHE = {
    "products": None,
    "updated_at": 0
}
EXT_EDO_CACHE = {
    "products": None,
    "updated_at": 0
}

EXT_OFD_MARKUP_PCT = float(os.getenv("EXTERNAL_OFD_MARKUP_PCT", 740))
EXT_SERVICES_MARKUP_PCT = float(os.getenv("EXTERNAL_SERVICES_MARKUP_PCT", 740))
EXT_EDO_MARKUP_PCT = float(os.getenv("EXTERNAL_EDO_MARKUP_PCT", 740))
#EXT_OFD_MARKUP_PCT = float(os.getenv("EXTERNAL_OFD_MARKUP_PCT", -99.99999))

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

# ===== Динамические товары (внешняя платформа) =====
def refresh_ext_ofd_products(force: bool = False):
    now = time.time()

    if (
        force
        or EXT_OFD_CACHE["products"] is None
        or now - EXT_OFD_CACHE["updated_at"] > EXT_OFD_TTL
    ):
        products = get_products('ofd')
        if not isinstance(products, dict):
            products = {}   # если баланс 0 -> get_products() возвращает []
        EXT_OFD_CACHE["products"] = products
        EXT_OFD_CACHE["updated_at"] = now

    return EXT_OFD_CACHE["products"]


def refresh_ext_service_products(force: bool = False):
    now = time.time()

    if (
        force
        or EXT_SERVICE_CACHE["products"] is None
        or now - EXT_SERVICE_CACHE["updated_at"] > EXT_OFD_TTL
    ):
        products = get_products("service")
        if not isinstance(products, dict):
            products = {}
        EXT_SERVICE_CACHE["products"] = products
        EXT_SERVICE_CACHE["updated_at"] = now

    return EXT_SERVICE_CACHE["products"]

def refresh_ext_service_orders_products(force: bool = False):
    now = time.time()

    if (
        force
        or EXT_SERVICE_ORDERS_CACHE["products"] is None
        or now - EXT_SERVICE_ORDERS_CACHE["updated_at"] > EXT_OFD_TTL
    ):
        products = get_products("services_for_orders")
        if not isinstance(products, dict):
            products = {}
        EXT_SERVICE_ORDERS_CACHE["products"] = products
        EXT_SERVICE_ORDERS_CACHE["updated_at"] = now

    return EXT_SERVICE_ORDERS_CACHE["products"]


def refresh_ext_edo_products(force: bool = False):
    now = time.time()

    if (
        force
        or EXT_EDO_CACHE["products"] is None
        or now - EXT_EDO_CACHE["updated_at"] > EXT_OFD_TTL
    ):
        products = get_products("edo")
        if not isinstance(products, dict):
            products = {}
        EXT_EDO_CACHE["products"] = products
        EXT_EDO_CACHE["updated_at"] = now

    return EXT_EDO_CACHE["products"]


def refresh_srv_orders_products(force: bool = False):
    now = time.time()

    if (
        force
        or EXT_EDO_CACHE["products"] is None
        or now - EXT_EDO_CACHE["updated_at"] > EXT_OFD_TTL
    ):
        products = get_products("services_for_orders")
        if not isinstance(products, dict):
            products = {}
        EXT_EDO_CACHE["products"] = products
        EXT_EDO_CACHE["updated_at"] = now

    return EXT_EDO_CACHE["products"]


# --- Наценка для внешних товаров (теперь через ext_products) ---

_EXT_PRICE_OVR_CACHE = {"data": None, "updated_at": 0.0}
_EXT_PRICE_OVR_TTL = 60 * 5  # 5 минут


async def _get_ext_products_pricing(force: bool = False) -> dict[tuple[str, int, int], dict]:
    """Кэш активных overrides: (category, operator_id, type_id) -> row."""
    now = time.time()
    if force or _EXT_PRICE_OVR_CACHE["data"] is None or (now - float(_EXT_PRICE_OVR_CACHE["updated_at"] or 0)) > _EXT_PRICE_OVR_TTL:
        rows = await get_ext_products_all(active_only=True)
        data: dict[tuple[str, int, int], dict] = {}
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


async def ext_sell_price(category: str, operator_id: int, type_id: int, source_price: float | int | None) -> int:
    """Цена продажи для внешнего товара.
    Приоритет:
      1) ext_products.price_fixed
      2) ext_products.markup_pct
      3) fallback env-проценты (EXTERNAL_*_MARKUP_PCT)
    """
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

    if ov and ov.get("price_fixed") is not None:
        try:
            return int(ov["price_fixed"])
        except Exception:
            pass

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



def _get_product(sku: str, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    # сначала статический каталог
    p = PRODUCTS.get(sku)
    if p:
        return p

    # потом динамика (если будет)
    dyn = (context.bot_data.get("dyn_products") or {}) if context else {}
    return dyn.get(sku)


def cart_kb(has_items: bool) -> InlineKeyboardMarkup:
    if has_items:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Оплатить", callback_data="checkout")],
            [InlineKeyboardButton("🧹 Очистить корзину", callback_data="clear_cart")],
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🧰 Услуги", callback_data="shop")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ])


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📲 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def order_success_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Мои покупки", callback_data="my_codes")],
        [InlineKeyboardButton("🛍 Купить ещё", callback_data="buy")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    text = (
        "Выберите раздел:"
    )
    
    cats0_eq = await get_equipment_categories(is_used=0, include_fn=True)
    context.user_data["eq_cats_0"] = cats0_eq
    fn_category = None
    for i, name in enumerate(cats0_eq or []):
        if name == "ФН":
            fn_category = i
            break
    if fn_category:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Коды ОФД", callback_data="services_ofd")],
            [InlineKeyboardButton("💾 ФН", callback_data=f"eq_cat:0:{fn_category}")],
            [InlineKeyboardButton("📄 ЭДО", callback_data="services_edo")],
            [InlineKeyboardButton("🛠 Торговое оборудование", callback_data="shop_equipment")],
            [InlineKeyboardButton("🛠 Лицензии и прошивки", callback_data="services_service")],
            [InlineKeyboardButton("🧻 Бумага", callback_data="shop_paper")],
            [InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:shop")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Коды ОФД", callback_data="services_ofd")],
            [InlineKeyboardButton("📄 ЭДО", callback_data="services_edo")],
            [InlineKeyboardButton("🛠 Торговое оборудование", callback_data="shop_equipment")],
            [InlineKeyboardButton("🛠 Лицензии и прошивки", callback_data="services_service")],
            [InlineKeyboardButton("🧻 Бумага", callback_data="shop_paper")],
            [InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:shop")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ])
    
    await send_or_edit(update, text, kb, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE

async def show_services_fn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"ФН 15 мес — {PRODUCTS['fn_15']['price']} ₽", callback_data="product_fn_15")],
        [InlineKeyboardButton(f"ФН 36 мес — {PRODUCTS['fn_36']['price']} ₽", callback_data="product_fn_36")],
        [InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:fn")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="buy")],
    ])

    await send_or_edit(update, "Выберите срок фискального накопителя:", kb, context=context)
    return STATE_IDLE

async def show_services_ofd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()

    # 1) Встроенные провайдеры (существующая логика выдачи кодов — НЕ ТРОГАЕМ)
    rows = [
        [InlineKeyboardButton(
            f"ОФД Платформа — {PRODUCTS['ofd_platform']['price']} ₽",
            callback_data="product_ofd_platform"
        )],
        [InlineKeyboardButton(
            f"Первый ОФД — {PRODUCTS['ofd_first']['price']} ₽",
            callback_data="product_ofd_first"
        )]
    ]
    
    products = refresh_ext_ofd_products()

    for idx, provider in enumerate(products.keys()):
        rows.append([InlineKeyboardButton(provider, callback_data=f"ext_ofd_p:{idx}")])

    # навигация
    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:ofd")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="buy")])

    kb = InlineKeyboardMarkup(rows)

    await send_or_edit(
        update,
        "🔑 <b>Коды ОФД</b>\n\n"
        "Выберите провайдера:",
        kb,
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE


async def show_services_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()

    products = refresh_ext_service_products()
    rows = []

    for idx, title in enumerate(products.keys()):
        rows.append([InlineKeyboardButton(title, callback_data=f"ext_srv_p:{idx}")])
    
    products_for_orders = refresh_ext_service_orders_products()
    for idx, title in enumerate(products_for_orders.keys()):
        rows.append([InlineKeyboardButton(title, callback_data=f"ext_srv_ord_p:{idx}")])
    
    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:service")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="buy")])

    await send_or_edit(
        update,
        "🛠 <b>Лицензии и прошивки</b>\n\nВыберите продукт:",
        InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE


async def ext_srv_provider_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    try:
        provider_idx = int(q.data.split(":", 1)[1])
    except Exception:
        return STATE_IDLE

    products = refresh_ext_service_products()
    providers = list(products.keys())

    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_service(update, context)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("service", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineKeyboardButton(
            f"{term_name} — {price} ₽",
            callback_data=f"ext_srv_add:{provider_idx}:{term_idx}"
        )])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:service")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="services_service")])

    await send_or_edit(
        update,
        f"Купить «<b>{provider}</b>»\nВыберите вариант:",
        InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE


async def ext_srv_ord_provider_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    try:
        provider_idx = int(q.data.split(":", 1)[1])
    except Exception:
        return STATE_IDLE

    products = refresh_ext_service_orders_products()
    providers = list(products.keys())

    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_service(update, context)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("services_for_orders", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineKeyboardButton(
            f"{term_name} — {price} ₽",
            callback_data=f"ext_srv_ord_add:{provider_idx}:{term_idx}"
        )])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:service_orders")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="services_service")])

    await send_or_edit(
        update,
        f"Купить «<b>{provider}</b>»\nВыберите вариант:",
        InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE



async def ext_srv_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = update.callback_query

    try:
        _, provider_idx, term_idx = q.data.split(":")
        provider_idx = int(provider_idx)
        term_idx = int(term_idx)
    except Exception:
        if q:
            await q.answer()
        return STATE_IDLE

    products = refresh_ext_service_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        if q:
            await q.answer()
        return await show_services_service(update, context)

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        if q:
            await q.answer()
        return await ext_srv_provider_cb(update, context)

    term_name, item = terms[term_idx]

    sell_price = await ext_sell_price("service", item["operatorId"], item["typeId"], item["price"])
    sku = f"service_ext:{item['operatorId']}:{item['typeId']}"

    cart = context.user_data.setdefault("cart", [])
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
        }
    })
    context.user_data["cart"] = cart

    if not added:
        await q.answer(f"❌ Нельзя добавить. {msg or ''}".strip(), show_alert=True)
        return STATE_PRODUCT_SELECT

    await q.answer()

    await send_or_edit(
        update,
        f"✅ *{item['name']}* добавлен в корзину.\n\nВы можете перейти в корзину, чтобы оплатить покупку, либо вернуться в магазин, чтобы добавить другие товары",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ]),
        parse_mode="Markdown",
        context=context
    )
    return STATE_PRODUCT_SELECT



async def ext_srv_ord_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = update.callback_query

    try:
        _, provider_idx, term_idx = q.data.split(":")
        provider_idx = int(provider_idx)
        term_idx = int(term_idx)
    except Exception:
        if q:
            await q.answer()
        return STATE_IDLE

    products = refresh_ext_service_orders_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        if q:
            await q.answer()
        return await show_services_service(update, context)

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        if q:
            await q.answer()
        return await ext_srv_provider_cb(update, context)

    term_name, item = terms[term_idx]

    sell_price = await ext_sell_price("services_for_orders", item["operatorId"], item["typeId"], item["price"])
    sku = f"service_ord_ext:{item['operatorId']}:{item['typeId']}"

    cart = context.user_data.setdefault("cart", [])
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
        }
    })
    context.user_data["cart"] = cart

    if not added:
        await q.answer(f"❌ Нельзя добавить. {msg or ''}".strip(), show_alert=True)
        return STATE_PRODUCT_SELECT

    await q.answer()

    user = await get_user(str(update.effective_user.id)) or {}
    buyer_kkt_factory_number = (user.get("kkt_factory_number") or "").strip()
    warning_text = (
        "<b>ВАЖНО!\n"
        "ВНИМАТЕЛЬНО ПРОЧТИ ИНФОРМАЦИЮ НИЖЕ!</b>\n\n"
        "- При заказе внимательно проверьте свои данные:\n"
        "ИНН клиента, заводской номер ККТ.\n"
        "- Возврат активированной лицензии НЕ возможен!!!\n"
        "- Внимательно проверяйте свои данные!!!\n"
        "-АТОЛ 90Ф не прошивается.\n\n"
        "В стоимость не входит работа тех. специалиста по прошивке ККТ. "
        "Формат выполнения и стоимость, мы согласуем отдельно и поможем с активацией прошивки АТОЛ Connect!!!\n\n"
        "После подтверждения  лицензии мы отправим вам последнюю версию прошивки, вам придёт уведомление. "
        "Вы сможете скачать файл прошивки, перейдя в раздел \"Заказы\", выбрав нужный заказ и перейдя в чат."
    )

    if not buyer_kkt_factory_number:
        context.user_data["co_mode"] = "service_orders_pick"
        await send_or_edit(
            update,
            (
                f"✅ <b>{item['name']}</b> добавлен в корзину.\n\n"
                f"{warning_text}\n\n"
                "Чтобы продолжить, введите заводской номер ККТ:"
            ),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
            context=context,
        )
        return STATE_CO_KKT_FACTORY_NUMBER

    context.user_data["confirmed_kkt_factory_number"] = buyer_kkt_factory_number

    await send_or_edit(
        update,
        (
            f"✅ <b>{item['name']}</b> добавлен в корзину.\n\n"
            f"{warning_text}\n\n"
            f"Текущий заводской номер ККТ: <b>{buyer_kkt_factory_number}</b>.\n"
            "Проверьте номер перед оплатой."
        ),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
            [InlineKeyboardButton("✏️ Изменить номер ККТ", callback_data="input_new_kkt_number")],
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ]),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_PRODUCT_SELECT

async def show_services_edo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()

    products = refresh_ext_edo_products()
    rows = []

    for idx, title in enumerate(products.keys()):
        rows.append([InlineKeyboardButton(title, callback_data=f"ext_edo_p:{idx}")])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:edo")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="buy")])

    await send_or_edit(
        update,
        "📄 <b>ЭДО</b>\n\nВыберите продукт:",
        InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE


async def ext_edo_provider_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    try:
        provider_idx = int(q.data.split(":", 1)[1])
    except Exception:
        return STATE_IDLE

    products = refresh_ext_edo_products()
    providers = list(products.keys())

    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_edo(update, context)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("edo", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineKeyboardButton(
            f"{term_name} — {price} ₽",
            callback_data=f"ext_edo_add:{provider_idx}:{term_idx}"
        )])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:edo")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="services_edo")])

    await send_or_edit(
        update,
        f"ЭДО «<b>{provider}</b>»\nВыберите вариант:",
        InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE

async def ext_edo_provider_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    try:
        provider_idx = int(q.data.split(":", 1)[1])
    except Exception:
        return STATE_IDLE

    products = refresh_ext_edo_products()
    providers = list(products.keys())

    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_edo(update, context)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("edo", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineKeyboardButton(
            f"{term_name} — {price} ₽",
            callback_data=f"ext_edo_add:{provider_idx}:{term_idx}"
        )])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:edo")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="services_edo")])

    await send_or_edit(
        update,
        f"ЭДО «<b>{provider}</b>»\nВыберите вариант:",
        InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE



async def ext_edo_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = update.callback_query

    try:
        _, provider_idx, term_idx = q.data.split(":")
        provider_idx = int(provider_idx)
        term_idx = int(term_idx)
    except Exception:
        if q:
            await q.answer()
        return STATE_IDLE

    products = refresh_ext_edo_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        if q:
            await q.answer()
        return await show_services_edo(update, context)

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        if q:
            await q.answer()
        return await ext_edo_provider_cb(update, context)

    term_name, item = terms[term_idx]

    sell_price = await ext_sell_price("edo", item["operatorId"], item["typeId"], item["price"])
    sku = f"edo_ext:{item['operatorId']}:{item['typeId']}"

    cart = context.user_data.setdefault("cart", [])
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
        }
    })
    context.user_data["cart"] = cart

    if not added:
        await q.answer(f"❌ Нельзя добавить. {msg or ''}".strip(), show_alert=True)
        return STATE_PRODUCT_SELECT

    await q.answer()

    await send_or_edit(
        update,
        f"✅ *{item['name']}* добавлен в корзину.\n\nВы можете перейти в корзину, чтобы оплатить покупку, либо вернуться в магазин, чтобы добавить другие товары",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ]),
        parse_mode="Markdown",
        context=context
    )
    return STATE_PRODUCT_SELECT

async def ext_ofd_provider_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    try:
        provider_idx = int(q.data.split(":", 1)[1])
    except Exception:
        return STATE_IDLE

    products = refresh_ext_ofd_products()
    providers = list(products.keys())

    if provider_idx < 0 or provider_idx >= len(providers):
        return await show_services_ofd(update, context)

    provider = providers[provider_idx]
    terms = products[provider]

    rows = []
    for term_idx, (term_name, item) in enumerate(terms.items()):
        price = await ext_sell_price("ofd", item["operatorId"], item["typeId"], item["price"])
        rows.append([InlineKeyboardButton(
            f"{term_name} — {price} ₽",
            callback_data=f"ext_ofd_add:{provider_idx}:{term_idx}"
        )])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:ofd")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="services_ofd")])

    await send_or_edit(
        update,
        f"ОФД «<b>{provider}</b>»\nВыберите срок:",
        InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
        context=context
    )
    return STATE_IDLE



async def ext_ofd_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = update.callback_query

    try:
        _, provider_idx, term_idx = q.data.split(":")
        provider_idx = int(provider_idx)
        term_idx = int(term_idx)
    except Exception:
        if q:
            await q.answer()
        return STATE_IDLE

    products = refresh_ext_ofd_products()
    providers = list(products.keys())
    if provider_idx < 0 or provider_idx >= len(providers):
        if q:
            await q.answer()
        return await show_services_ofd(update, context)

    provider = providers[provider_idx]
    terms = list(products[provider].items())
    if term_idx < 0 or term_idx >= len(terms):
        if q:
            await q.answer()
        return await ext_ofd_provider_cb(update, context)

    term_name, item = terms[term_idx]

    sell_price = await ext_sell_price("ofd", item["operatorId"], item["typeId"], item["price"])
    sku = f"ofd_ext:{item['operatorId']}:{item['typeId']}"

    cart = context.user_data.setdefault("cart", [])
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
        }
    })
    context.user_data["cart"] = cart

    if not added:
        await q.answer(f"❌ Нельзя добавить. {msg or ''}".strip(), show_alert=True)
        return STATE_PRODUCT_SELECT

    await q.answer()

    await send_or_edit(
        update,
        f"✅ *{item['name']}* добавлен в корзину.\n\nВы можете перейти в корзину, чтобы оплатить покупку, либо вернуться в магазин, чтобы добавить другие товары",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ]),
        parse_mode="Markdown",
        context=context
    )
    return STATE_PRODUCT_SELECT

async def show_shop_equipment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Раздел «Торговое оборудование» (таблица equipment).

    Дерево:
      - {категории}
      - Б/у (is_used=1)
          - {категории}
    """
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    cats0 = await get_equipment_categories(is_used=0)
    context.user_data["eq_cats_0"] = cats0

    _paper = {"Кассовые ленты", "Этикетки"}
    rows: list[list[InlineKeyboardButton]] = []
    for i, cat in enumerate(cats0 or []):
        if str(cat) not in _paper:
            rows.append([InlineKeyboardButton(str(cat), callback_data=f"eq_cat:0:{i}")])

    # подменю «Б/у»
    rows.append([InlineKeyboardButton("♻️ Б/у", callback_data="eq_root:1")])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:equipment")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="buy")])

    text = "🛠 <b>Торговое оборудование</b>\n\nВыберите категорию:"
    kb = InlineKeyboardMarkup(rows)

    await send_or_edit(update, text, kb, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE


async def show_shop_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Раздел «Бумага»: категории «Кассовые ленты» и «Этикетки»."""
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    cats0 = await get_equipment_categories(is_used=0)
    context.user_data["eq_cats_0"] = cats0
    paper_categories = {"Кассовые ленты", "Этикетки"}

    rows: list[list[InlineKeyboardButton]] = []
    for i, cat in enumerate(cats0 or []):
        if str(cat) in paper_categories:
            rows.append([InlineKeyboardButton(str(cat), callback_data=f"eq_cat:0:{i}")])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:paper")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="buy")])

    text = "🧻 <b>Бумага</b>\n\nВыберите категорию:"
    if not any(str(cat) in paper_categories for cat in (cats0 or [])):
        text = "🧻 <b>Бумага</b>\n\nКатегории «Кассовые ленты» и «Этикетки» пока не добавлены."

    kb = InlineKeyboardMarkup(rows)
    await send_or_edit(update, text, kb, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE


async def eq_root_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переход в корень equipment по is_used."""
    q = update.callback_query
    await q.answer()

    try:
        is_used = int(q.data.split(":", 1)[1])
    except Exception:
        is_used = 0

    if is_used == 0:
        return await show_shop_equipment(update, context)

    cats = await get_equipment_categories(is_used=1)
    context.user_data["eq_cats_1"] = cats

    rows: list[list[InlineKeyboardButton]] = []
    for i, cat in enumerate(cats or []):
        rows.append([InlineKeyboardButton(str(cat), callback_data=f"eq_cat:1:{i}")])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:equipment")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="shop_equipment")])

    text = "♻️ <b>Б/у оборудование</b>\n\nВыберите категорию:"
    kb = InlineKeyboardMarkup(rows)

    # если текущее сообщение с фото — удаляем и отправляем новое текстовое меню
    if q and q.message and getattr(q.message, "photo", None):
        chat_id = q.message.chat_id
        try:
            await q.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return STATE_IDLE

    await send_or_edit(update, text, kb, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE


async def eq_category_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Список товаров в выбранной категории equipment."""
    q = update.callback_query
    await q.answer()

    try:
        _, is_used_s, idx_s = q.data.split(":", 2)
        is_used = int(is_used_s)
        idx = int(idx_s)
    except Exception:
        return await show_shop_equipment(update, context)

    cats = context.user_data.get(f"eq_cats_{is_used}") or await get_equipment_categories(is_used=is_used)
    context.user_data[f"eq_cats_{is_used}"] = cats

    if idx < 0 or idx >= len(cats):
        return await (eq_root_cb(update, context) if is_used else show_shop_equipment(update, context))

    category = cats[idx]
    items = await get_equipment_items(str(category), is_used=is_used)

    rows: list[list[InlineKeyboardButton]] = []
    for it in items or []:
        try:
            item_id = int(it.get("id"))
        except Exception:
            continue
        name = str(it.get("name") or "Товар")
        price = int(it.get("price") or 0)
        rows.append([InlineKeyboardButton(f"{name} — {price} ₽", callback_data=f"eq_view:{is_used}:{idx}:{item_id}")])

    rows.append([InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:equipment")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=("eq_root:1" if is_used else "shop_equipment"))])

    title = "♻️" if is_used else "🛠"
    text = f"{title} <b>{category}</b>\n\nВыберите товар:"
    kb = InlineKeyboardMarkup(rows)

    if q and q.message and getattr(q.message, "photo", None):
        chat_id = q.message.chat_id
        try:
            await q.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return STATE_IDLE

    await send_or_edit(update, text, kb, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE


def _equipment_image_path(image_name: str) -> str:
    """Файлы лежат в /bot/images/ (т.е. ./images относительно этого файла)."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "images", image_name)


async def eq_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Карточка товара equipment: фото + добавить/назад."""
    q = update.callback_query
    await q.answer()

    try:
        _, is_used_s, idx_s, item_id_s = q.data.split(":", 3)
        is_used = int(is_used_s)
        idx = int(idx_s)
        item_id = int(item_id_s)
    except Exception:
        return STATE_IDLE

    it = await get_equipment_by_id(item_id)
    if not it:
        await q.answer("Товар не найден.", show_alert=True)
        return await eq_category_cb(update, context)

    name = str(it.get("name") or "Товар")
    price = int(it.get("price") or 0)
    image_name = (it.get("image_name") or "").strip()

    # наличие
    avail = await get_equipment_available_count(item_id)
    if avail is None:
        stock_line = "В наличии: <b>без ограничений</b>"
        can_buy = True
    else:
        stock_line = f"В наличии: <b>{int(avail)}</b>"
        can_buy = int(avail) > 0

    caption = f"🛠 <b>{name}</b>\nЦена: <b>{price} ₽</b>\n{stock_line}"

    btn_add = InlineKeyboardButton("➕ Добавить в корзину", callback_data=f"eq_add:{is_used}:{idx}:{item_id}") if can_buy else InlineKeyboardButton("⛔️ Нет в наличии", callback_data="cart_noop")
    kb = InlineKeyboardMarkup([
        [btn_add],
        [InlineKeyboardButton("💬 Консультация", callback_data="shop_consult:equipment")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"eq_cat:{is_used}:{idx}")],
    ])

    img_path = _equipment_image_path(str(image_name)) if image_name else ""

    chat_id = q.message.chat_id
    try:
        if image_name and os.path.exists(img_path):
            with open(img_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption + ("\n\n(Фото не найдено)" if image_name else ""),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )

        # удаляем список
        try:
            await q.message.delete()
        except Exception:
            pass

    except Exception:
        await q.edit_message_text(
            caption + ("\n\n(Фото не найдено)" if image_name and not os.path.exists(img_path) else ""),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    return STATE_IDLE


async def eq_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    """Добавляет выбранный товар equipment в корзину (с учётом остатков)."""
    q = update.callback_query
    await q.answer()

    try:
        _, is_used_s, idx_s, item_id_s = q.data.split(":", 3)
        is_used = int(is_used_s)
        item_id = int(item_id_s)
    except Exception:
        return STATE_PRODUCT_SELECT

    it = await get_equipment_by_id(item_id)
    if not it:
        await q.answer("Товар не найден", show_alert=True)
        return STATE_PRODUCT_SELECT

    # дополнительная защита: если count задан и <=0 — не добавляем
    avail = await get_equipment_available_count(item_id)
    if avail is not None and int(avail) < 1:
        await q.answer("⛔️ Нет в наличии", show_alert=True)
        return STATE_PRODUCT_SELECT

    name = str(it.get("name") or "Товар")
    price = int(it.get("price") or 0)
    sku = f"equip_{item_id}"

    cart = context.user_data.setdefault("cart", [])
    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": name,
        "price": price,
        "qty": 1,
        "meta": {"equipment_id": item_id, "is_used": int(it.get("is_used") or 0)},
    })
    context.user_data["cart"] = cart

    if not added:
        await q.answer(f"❌ Нельзя добавить. {msg or ''}".strip(), show_alert=True)
        return STATE_PRODUCT_SELECT

    await send_or_edit(
        update,
        f"✅ *{name}* добавлен в корзину.\n\nВы можете перейти в корзину, чтобы оплатить покупку, либо вернуться в магазин, чтобы добавить другие товары",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ]),
        parse_mode="Markdown",
        context=context
    )
    return STATE_PRODUCT_SELECT

async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = update.callback_query
    if not _debounce_cb(update, context):
        if q:
            await q.answer()
        return STATE_PRODUCT_SELECT

    sku = q.data.split("product_", 1)[1]  # например, "fn_15"
    p = PRODUCTS.get(sku)
    if not p:
        await q.answer("Товар не найден.", show_alert=True)
        return STATE_PRODUCT_SELECT

    cart = context.user_data.setdefault("cart", [])

    added, msg = await _cart_try_add_line(cart, {
        "sku": sku,
        "name": p["name"],
        "price": p["price"],
        "qty": 1,
        # ФН теперь категория оборудования, но остаётся в отдельном интерфейсе магазина.
        # Привязываем к equipment.id, чтобы в заказе сохранялся equipment_id.
        "meta": (
            {"equipment_id": await get_equipment_id_by_name_category(p["name"], "fn")}
            if str(sku).startswith("fn_") else None
        ),
    })

    context.user_data["cart"] = cart

    if not added:
        await q.answer(f"❌ Нельзя добавить. {msg or ''}".strip(), show_alert=True)
        return STATE_PRODUCT_SELECT

    await q.answer()

    await q.edit_message_text(
        f"✅ *{p['name']}* добавлен в корзину.\n\nВы можете перейти в корзину, чтобы оплатить покупку, либо вернуться в магазин, чтобы добавить другие товары",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
            [InlineKeyboardButton("⬅️ К магазину", callback_data="buy")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
        ]),
        parse_mode="Markdown"
    )

    return STATE_PRODUCT_SELECT

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

def _cart_total(cart: list[dict]) -> int:
    return sum(int(x.get("price") or 0) * int(x.get("qty") or 1) for x in (cart or []))

def _cart_item_title(it: dict) -> str:
    # подстрахуемся под разные ключи
    return (
        it.get("item_title")
        or it.get("title")
        or it.get("name")
        or "Товар"
    )

def _kb_cart(cart: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # Кнопки управления каждой строкой корзины
    for i, it in enumerate(cart or []):
        qty = int(it.get("qty") or 1)
        # Мини-контролы: - qty +
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"cart_dec:{i}"),
            InlineKeyboardButton(f" {qty} ", callback_data="cart_noop"),
            InlineKeyboardButton("➕", callback_data=f"cart_inc:{i}"),
            InlineKeyboardButton("🗑", callback_data=f"del_{i}"),
        ])

    # Нижние кнопки
    if cart:
        rows.append([InlineKeyboardButton("✅ Оформить", callback_data="checkout")])
        rows.append([InlineKeyboardButton("🧹 Очистить корзину", callback_data="clear_cart")])

    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="refresh")])
    return InlineKeyboardMarkup(rows)


# ===== Ограничение количества по наличию (защита от заказа больше, чем есть) =====

def _normalize_ext_sku(sku: str) -> str | None:
    """Нормализуем внешний SKU до 'prefix:operatorId:typeId' (без orderId)."""
    ids = _parse_ext_any_ids((sku or "").strip())
    if not ids:
        return None
    prefix, op, tp, _oid = ids
    return f"{prefix}:{op}:{tp}"

def _stock_key_for_sku(sku: str) -> tuple[str | None, str | None]:
    """Возвращает (kind, key):
    - ('pool', 'ofd_first'|'ofd_platform') для наших пулов кодов
    - ('ext', 'prefix:op:tp') для внешних кодов
    - (None, None) если товар не ограничиваем (нет понятного остатка)
    """
    s = (sku or "").lower().strip()

    ext = _normalize_ext_sku(s)
    if ext:
        return "ext", ext

    # equipment (товары из таблицы equipment)
    if s.startswith("equip_"):
        try:
            eid = int(s.split("_", 1)[1])
            return "equip", str(eid)
        except Exception:
            return None, None

    # наши пулы кодов (внутренние)
    if s == "ofd_platform" or "ofd_platform" in s or "platform" in s:
        return "pool", "ofd_platform"
    if s == "ofd_first" or s.startswith("ofd_first"):
        return "pool", "ofd_first"

    # исторические значения SKU (если где-то сохранились) — приводим к новой схеме
    if s == "ofd" or s.startswith("ofd"):
        return "pool", "ofd_first"

    return None, None

def _cart_sum_by_stock_key(cart: list[dict], kind: str, key: str, exclude_idx: int | None = None) -> int:
    total = 0
    for i, it in enumerate(cart or []):
        if exclude_idx is not None and i == exclude_idx:
            continue
        k_kind, k_key = _stock_key_for_sku(str(it.get("sku") or ""))
        if k_kind == kind and k_key == key:
            total += int(it.get("qty") or 1)
    return total

async def _available_for_stock_key(kind: str, key: str, *, force: bool = False) -> int:
    """Сколько доступно сейчас по ключу наличия."""
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
        # key: 'prefix:op:tp'
        parts = (key or "").split(":")
        if len(parts) < 3:
            return 0
        prefix, op, tp = parts[0], parts[1], parts[2]

        if prefix == "service_ext":
            products = refresh_ext_service_products(force=force)
        elif prefix == "service_ord_ext":
            products = refresh_ext_service_orders_products(force=force)
        elif prefix == "edo_ext":
            products = refresh_ext_edo_products(force=force)
        else:
            products = refresh_ext_ofd_products(force=force)

        _vendor, prod = _find_ext_ofd_product(products, op, tp)
        if prefix == "service_ord_ext":
            return 10**9
        try:
            cnt = (prod or {}).get("count")
            if (cnt is None):
                return 10**9
            return int(cnt or 0)
        except Exception:
            return 0

    return 10**9

async def _validate_cart_stock(cart: list[dict]) -> list[dict]:
    """Проверяет корзину. Возвращает список проблем:
    [{kind,key,need,available}, ...]
    """
    req: dict[tuple[str, str], int] = {}
    for it in cart or []:
        kind, key = _stock_key_for_sku(str(it.get("sku") or ""))
        if not kind or not key:
            continue
        req[(kind, key)] = req.get((kind, key), 0) + int(it.get("qty") or 1)

    problems: list[dict] = []
    for (kind, key), need in req.items():
        avail = await _available_for_stock_key(kind, key, force=True)
        if need > avail:
            problems.append({"kind": kind, "key": key, "need": need, "available": avail})
    return problems

async def _cart_try_inc(cart: list[dict], idx: int) -> tuple[bool, str | None]:
    """Пытается увеличить qty в строке idx на 1 с учётом наличия.
    Возвращает (changed, msg_if_blocked).
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

async def _cart_try_add_line(cart: list[dict], line: dict) -> tuple[bool, str | None]:
    """Добавить 1 шт. товара (или увеличить существующую строку с тем же SKU),
    но не превышая наличие.
    """
    sku = str(line.get("sku") or "").strip()
    if not sku:
        return False, "Некорректный товар"

    kind, key = _stock_key_for_sku(sku)
    add_qty = int(line.get("qty") or 1)

    # Мержим в существующую строку с тем же SKU (аккуратно, чтобы не плодить дубликаты)
    same_idx = None
    for i, it in enumerate(cart):
        if str(it.get("sku") or "") == sku:
            same_idx = i
            break

    if kind and key:
        avail = await _available_for_stock_key(kind, key, force=True)
        already = _cart_sum_by_stock_key(cart, kind, key)
        if already + add_qty > avail:
            return False, f"Доступно: {avail} шт."

    if same_idx is not None:
        cart[same_idx]["qty"] = int(cart[same_idx].get("qty") or 1) + add_qty
    else:
        line = dict(line)
        line["qty"] = add_qty
        cart.append(line)

    return True, None

def _format_cart_text(cart: list[dict], notice: str | None = None) -> str:
    if not cart:
        return "Корзина пуста."

    lines: list[str] = []
    if notice:
        lines.append(notice)
        lines.append("")
    lines.append("🛒 <b>Корзина</b>\n")

    for idx, it in enumerate(cart, start=1):
        title = _cart_item_title(it)
        price = int(it.get("price") or 0)
        qty = int(it.get("qty") or 1)
        lines.append(f"{idx}) {title} — <b>{qty} шт.</b> × {price} ₽ = <b>{qty * price} ₽</b>")

    total = _cart_total(cart)
    lines.append(f"\n<b>Итого:</b> {total} ₽")
    lines.append("\n<i>Меняй количество кнопками ➖/➕ или удаляй строку 🗑</i>")
    return "\n".join(lines)



async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    cart = context.user_data.get("cart") or []

    if not cart:
        text = "🛒 <b>Корзина пуста</b>."
        await send_or_edit(
            update,
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="refresh")]]),
            parse_mode=ParseMode.HTML
        )
        return STATE_CART

    text = _format_cart_text(cart)
    await send_or_edit(update, text, reply_markup=_kb_cart(cart), parse_mode=ParseMode.HTML)
    return STATE_CART

async def clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    context.user_data["cart"] = []
    return await show_cart(update, context)

async def remove_from_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    data = (q.data or "") if q else ""
    # ожидаем del_{index}
    try:
        idx = int(data.split("_", 1)[1])
    except Exception:
        return await show_cart(update, context)

    cart = context.user_data.get("cart") or []
    if 0 <= idx < len(cart):
        cart.pop(idx)
    context.user_data["cart"] = cart

    return await show_cart(update, context)


async def cart_inc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = update.callback_query

    data = q.data or ""
    try:
        idx = int(data.split(":", 1)[1])
    except Exception:
        if q:
            await q.answer()
        return await show_cart(update, context)

    cart = context.user_data.get("cart") or []
    if not (0 <= idx < len(cart)):
        if q:
            await q.answer()
        return await show_cart(update, context)

    changed, msg = await _cart_try_inc(cart, idx)
    context.user_data["cart"] = cart

    if not changed and q:
        await q.answer(msg or "Недостаточно в наличии", show_alert=True)
    else:
        if q:
            await q.answer()

    return await show_cart(update, context)

async def cart_dec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = update.callback_query
    await q.answer()

    data = q.data or ""
    try:
        idx = int(data.split(":", 1)[1])
    except Exception:
        return await show_cart(update, context)

    cart = context.user_data.get("cart") or []
    if 0 <= idx < len(cart):
        cur = int(cart[idx].get("qty") or 1) - 1
        if cur <= 0:
            cart.pop(idx)
        else:
            cart[idx]["qty"] = cur
    context.user_data["cart"] = cart
    return await show_cart(update, context)

async def cart_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # кнопка "кол-во" чтобы не ругался Telegram на пустой callback
    q = update.callback_query
    if q:
        await q.answer()
    return STATE_CART



# ===== Оплата / Tinkoff / выдача кодов =====
TINKOFF_TAXATION = os.getenv("TINKOFF_TAXATION", "usn_income").strip()

COMPANY_EMAIL = (os.getenv("COMPANY_EMAIL", "") or "").strip() or None  # напр. billing@your.ru
COMPANY_INN   = (os.getenv("COMPANY_INN", "") or "").strip() or None     # напр. 7700000000


def make_receipt_items(cart: list[dict]) -> list[dict]:
    """
    cart: [{"name": "...", "price": 100, "qty": 2}, ...]  # цена в рублях
    -> items для Тинькофф в КОПЕЙКАХ.
    """
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
        })
    return items

def _receipt_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    d = "".join(ch for ch in phone if ch.isdigit())
    if len(d) == 10 and d.startswith("9"):
        d = "7" + d
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    return d if len(d) == 11 and d.startswith("7") else None



def _find_ext_product(products: dict, operator_id: str, type_id: str):
    """Ищет товар в дереве {vendor:{term:{...}}} по operatorId/typeId."""
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

async def _cart_to_order_items_reserved(cart: list[dict], user_id: str, tester_mode: bool = False) -> list[dict]:
    """Преобразует корзину в строки заказа И СРАЗУ бронирует нужные коды в БД.

    Возвращаемые элементы подходят для save_order_items():
    - для оборудования: equipment_id
    - для кодов: code_id (уже забронированная строка в таблице codes)
    """
    out: list[dict] = []

    # кэши внешних товаров (для vendor/source_price/months)
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

        # внешние товары (ofd/service/edo)
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
                    #сначала проверяем наличие уже купленных кодов в БД
                    code_id = await reserve_pool_code_for_user(user_id, ct, operator_id=operator_id, type_id=type_id, sell_price=price)
                    if not(code_id):
                        if tester_mode:
                            ext_order_id = f"TEST_RESERVE:{prefix}:{operator_id}:{type_id}:{user_id}"
                        else:
                            reserve_ans = await _run_in_thread(
                                reserve_ofd_order,
                                str(operator_id),
                                str(type_id),
                                1
                            )
                            ext_order_id = ((reserve_ans or {}).get("orderId") or "").strip()
                            if not ext_order_id:
                                raise RuntimeError(f"reserve_codes() вернул пустой orderId для {prefix}:{operator_id}/{type_id}")

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

        # неизвестный sku: пропускаем
        continue

    return out
    
def _make_items_from_cart(cart: list[dict]) -> list[dict]:
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
            "MeasurementUnit": "piece",  # 👈 ОБЯЗАТЕЛЬНО для FFD 1.2
        })
    return items

def build_receipt(user: dict, items: list[dict]) -> dict:
    """
    user: {"email": "...", "phone": "..."} — можно передавать и больше полей, лишнее игнорируется.
    items: уже в копейках и в формате Тинькофф (см. make_receipt_items).
    """
    # адрес чека (получатель)
    buyer_email = (user.get("email") or "").strip() or None
    buyer_phone = _receipt_phone(user.get("phone"))

    rec = {
        "Taxation": TINKOFF_TAXATION,   # osn | usn_income | usn_income_outcome | envd | esn | patent
        "Items": items,
    }
    if buyer_email:
        rec["Email"] = buyer_email
    if buyer_phone:
        rec["Phone"] = buyer_phone
    if COMPANY_EMAIL:
        rec["EmailCompany"] = COMPANY_EMAIL

    # ОБЯЗАТЕЛЬНО для SendReceipt: чем оплачено и на какую сумму
    total_kop = sum(int(i.get("Amount") or 0) for i in items)
    rec["Payments"] = {"Electronic": total_kop}

    # COMPANY_INN можно добавить как SupplierInfo у каждой позиции (не обязательно):
    if COMPANY_INN:
        for it in rec["Items"]:
            it.setdefault("SupplierInfo", {"Inn": COMPANY_INN})

    return rec

def _build_fallback_receipt_for_cashbox(
    *, amount_kop: int, order_row: dict, item_title: str | None, taxation: str,
    company_email: str | None
) -> dict:
    email = (order_row.get("buyer_email") or "").strip() or None
    phone = _receipt_phone(order_row.get("buyer_phone"))
    name = (item_title or "Оплата заказа")[:128]

    items = [{
        "Name": name,
        "Price": amount_kop,
        "Quantity": 1,
        "Amount": amount_kop,
        "Tax": "none",
        "PaymentMethod": "full_payment",
        "PaymentObject": "service",
        "MeasurementUnit": "piece",  # 👈 тоже добавляем
    }]


    rcpt = {"Taxation": taxation, "Items": items}
    if email:
        rcpt["Email"] = email
    if phone:
        rcpt["Phone"] = phone
    if company_email:
        rcpt["EmailCompany"] = company_email
    # Для cashbox SendClosingReceipt Payments ОБЯЗАТЕЛЕН — ставим безнал на всю сумму:
    rcpt["Payments"] = {"Electronic": amount_kop}
    return rcpt



async def send_notification_about_order(order_id: int, client_name: str, context: ContextTypes.DEFAULT_TYPE):
    directors = await list_directors()
    for d in directors:
        if not isinstance(d, dict):
            continue

        tg_to = d.get("tg_user_id")
        if not tg_to:
            continue
        
        await context.application.bot.send_message(
                chat_id=int(tg_to),
                text=f"Клиент {client_name} оформил новый заказ",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Смотреть заказ", callback_data=f"show_order:{order_id}")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]
                ]),
                disable_web_page_preview=True,
            )


async def _build_payment_success_text(context: ContextTypes.DEFAULT_TYPE, order_row: dict, items: list[dict], codes: dict, user_first_name:str, detailed_codes: list[dict] | None = None) -> str:
    """
    items: строки order_items
    codes: {'ofd_first': [...], 'ofd_platform': [...], 'ofd_other': [...], 'service': [...], 'edo': [...]} (как выдаёт issue_code_for_order)
    detailed_codes: список строк из get_order_codes_detailed(order_id)
    """
    lines: list[str] = ["Оплата подтверждена ✅"]

    # какие позиции были в заказе (новая схема)
    has_equip = any((i.get("equipment_id") is not None) for i in (items or []))
    has_ofd_first = any((i.get("code_type") or "").lower() == "ofd_first" for i in (items or []))
    has_ofd_platform = any((i.get("code_type") or "").lower() == "ofd_platform" for i in (items or []))
    has_ext_codes = any((i.get("code_type") or "").lower() in ("ofd_other", "service", "edo") for i in (items or []))
    has_ext_serv_orders = any((i.get("code_type") or "").lower() == "service_order" for i in (items or []))

    # 1) Если есть детальные коды из БД — показываем их (самый правильный вариант)
    if detailed_codes:
        lines.append("")
        lines.append("Ваши коды:")

        for r in detailed_codes:
            # максимально безопасно под разные форматы
            code_val = (
                (r.get("code_value") if isinstance(r, dict) else None)
                or (r.get("code") if isinstance(r, dict) else None)
                or (r.get("value") if isinstance(r, dict) else None)
                or ""
            )
            code_val = str(code_val).strip()
            if not code_val:
                continue

            name = (
                (r.get("item_name") if isinstance(r, dict) else None)
                or (r.get("item_title") if isinstance(r, dict) else None)
                or (r.get("title") if isinstance(r, dict) else None)
                or ""
            )
            name = str(name).strip()

            code_type = (r.get("code_type") if isinstance(r, dict) else None)
            code_type = str(code_type).strip().lower() if code_type else ""

            if not name:
                # fallback по типу кода
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

            lines.append(f"• {name}: <code>{code_val}</code>")

    else:
        # 2) Fallback: без детальных строк из БД показываем коды по типам (как вернул issue_code_for_order)
        ofd_codes = list(codes.get("ofd_first") or [])
        plat_codes = list(codes.get("ofd_platform") or [])
        ext_ofd = list(codes.get("ofd_other") or [])
        svc = list(codes.get("service") or [])
        edo = list(codes.get("edo") or [])

        if ofd_codes or plat_codes or ext_ofd or svc or edo:
            lines.append("")
            lines.append("Ваши коды:")
            for c in ofd_codes:
                lines.append(f"• ОФД: <code>{c}</code>")
            for c in ext_ofd:
                lines.append(f"• ОФД (внешний): <code>{c}</code>")
            for c in plat_codes:
                lines.append(f"• ОФД Платформа: <code>{c}</code>")
            for c in svc:
                lines.append(f"• Услуга: <code>{c}</code>")
            for c in edo:
                lines.append(f"• ЭДО: <code>{c}</code>")

        if (has_ofd_first or has_ofd_platform or has_ext_codes) and not (ofd_codes or plat_codes or ext_ofd or svc or edo):
            lines.append("")
            lines.append("Коды ОФД временно недоступны — заказ зафиксирован, мы передадим коды позже.")

    # 3) оборудование и коды на заказ
    if has_equip:
        lines.append("")
        lines.append("Заказ на оборудование зафиксирован — мы свяжемся с вами для уточнения деталей.")
    if has_ext_serv_orders:
        lines.append("")
        lines.append("Заказы на коды зафиксированы — мы свяжемся с вами для уточнения деталей.")
    if has_ext_serv_orders or has_equip:
        await send_notification_about_order(order_row["id"], user_first_name, context)


    return "\n".join(lines)


async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_commerce(update, context):
        return STATE_IDLE

    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    uid = str(update.effective_user.id)
    tg_username = (update.effective_user.username or "").strip()

    cart = context.user_data.get("cart") or []
    if not cart:
        await send_or_edit(update, "Корзина пуста.", kb_services(), parse_mode=ParseMode.HTML)
        return STATE_CART


    # 0) Проверяем наличие (наши пулы + внешняя платформа). Если не хватает — не даём оформить.
    problems = await _validate_cart_stock(cart)
    if problems:
        lines = ["❌ <b>Недостаточно товара в наличии.</b>\n"]
        for p in problems:
            kind = p.get("kind")
            key = p.get("key")
            need = int(p.get("need") or 0)
            avail = int(p.get("available") or 0)

            if kind == "pool":
                title = "Наши коды: " + ("ОФД Платформа" if key == "ofd_platform" else "ОФД")
            else:
                title = "Внешний товар: " + str(key)

            lines.append(f"• {title}: нужно <b>{need}</b>, доступно <b>{avail}</b>")

        notice = "\n".join(lines) + "\n\nУменьшите количество в корзине и попробуйте снова."
        await send_or_edit(
            update,
            _format_cart_text(cart, notice=notice),
            reply_markup=_kb_cart(cart),
            parse_mode=ParseMode.HTML,
            context=context
        )
        return STATE_CART

    # гарантируем, что user есть в БД (минимум tg_username)
    await upsert_user(uid, first_name=update.effective_user.first_name or "", tg_username=tg_username)

    user = await get_user(uid) or {}
    buyer_name = (user.get("first_name") or "").strip() or (update.effective_user.first_name or "")
    buyer_phone = (user.get("phone") or "").strip() or None
    buyer_email = (user.get("email") or "").strip() or None
    buyer_kkt_factory_number = (user.get("kkt_factory_number") or "").strip() or None

    is_tester_checkout = is_tester(uid)

    have_services_orders = any("service_ord_ext" in str(it.get("sku") or "") for it in (cart or []))

    if have_services_orders:
        # Номер ККТ обязателен для service_ord_ext
        if not buyer_kkt_factory_number:
            context.user_data["co_mode"] = "checkout"
            await send_or_edit(
                update,
                "Чтобы оформить заказ, нужен заводской номер ККТ.\n\nВведите заводской номер ККТ:",
                reply_markup=None,
                parse_mode=ParseMode.HTML,
            )
            return STATE_CO_KKT_FACTORY_NUMBER

        # ✅ Вместо bool-флага: запоминаем, какой номер пользователь подтверждал
        current = str(buyer_kkt_factory_number or "").strip()
        confirmed = str(context.user_data.get("confirmed_kkt_factory_number") or "").strip()

        # Если этот номер ещё не подтверждали (или номер изменился) — спрашиваем подтверждение
        if not confirmed or confirmed != current:
            context.user_data["co_mode"] = "checkout"
            # сохраняем "кандидата", который подтверждаем (чтобы при смене номера не залипало)
            context.user_data["confirmed_kkt_factory_number"] = current

            await send_or_edit(
                update,
                f"Сохранённый номер ККТ: <b>{current}</b>.\n\nЭто правильный номер?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Да", callback_data="checkout")],
                    [InlineKeyboardButton("Нет, ввести другой", callback_data="input_new_kkt_number")],
                ]),
                parse_mode=ParseMode.HTML,
            )
            return STATE_CART
        
    # Email is required to send a receipt (чек). Ask user before creating order/payment.
    if not buyer_email:
        context.user_data["co_mode"] = "checkout"
        await send_or_edit(
            update,
            "Для оплаты нужен <b>email</b>, чтобы вам пришёл чек.\n\nВведите email:",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В корзину", callback_data="cart")]]),
            parse_mode=ParseMode.HTML,
        )
        return STATE_CO_EMAIL
    context.user_data.pop("co_mode", None)

    # Запрашиваем адрес доставки только если в корзине есть оборудование
    _has_equip_in_cart = any((it.get("meta") or {}).get("equipment_id") is not None for it in cart)
    if _has_equip_in_cart and not context.user_data.get("_co_address_decided"):
        saved_address = (user.get("address") or "").strip() or None
        if saved_address:
            await send_or_edit(
                update,
                f"Адрес доставки: <b>{saved_address}</b>\n\nИспользовать этот адрес?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Да, использовать", callback_data="co_addr_use")],
                    [InlineKeyboardButton("✏️ Ввести другой", callback_data="co_addr_new")],
                ]),
                parse_mode=ParseMode.HTML,
                context=context,
            )
        else:
            await send_or_edit(
                update,
                "Введите адрес доставки:",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В корзину", callback_data="cart")]]),
                parse_mode=ParseMode.HTML,
            )
        context.user_data["co_mode"] = "checkout"
        return STATE_CO_ADDRESS
    total_rub = sum(int(x.get("price") or 0) * int(x.get("qty") or 1) for x in cart)

    # Предложение списания баллов (если ещё не ответил)
    if not context.user_data.get("_co_bonus_decided"):
        try:
            from services.checkout_bonus import get_bonus_checkout_info
            from services.bot_loyalty_ui import format_checkout_bonus_info
            from db.users import resolve_internal_user_id
            _iid = await resolve_internal_user_id(tg_user_id=int(uid))
            if _iid:
                bonus_info = await get_bonus_checkout_info(_iid, total_rub)
                if bonus_info.get("available"):
                    context.user_data["_co_bonus_info"] = bonus_info
                    context.user_data["_co_bonus_iid"] = _iid
                    usable = bonus_info.get("usable_points", 0)
                    discount = bonus_info.get("usable_rub_discount", 0)
                    text = (
                        format_checkout_bonus_info(bonus_info, html=True)
                        + f"\n\nИтого к оплате: <b>{total_rub} ₽</b>"
                        + f"\nСо скидкой: <b>{total_rub - discount:.0f} ₽</b>"
                    )
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"✅ Списать {usable} балл. (−{discount:.0f} ₽)", callback_data="co_bonus_use")],
                        [InlineKeyboardButton("❌ Не списывать", callback_data="co_bonus_skip")],
                    ])
                    await send_or_edit(update, text, kb, parse_mode=ParseMode.HTML, context=context)
                    return STATE_CO_BONUS
        except Exception:
            pass
        # Если лояльность недоступна — продолжаем без неё
        context.user_data["_co_bonus_decided"] = True

    # 1) создаём шапку заказа в БД (возвращает id заказа в новой схеме)
    context.user_data.pop("_co_address_decided", None)
    order_id_int = await create_order(
        user_id=uid,
        amount=total_rub,
        buyer_name=buyer_name,
        buyer_phone=buyer_phone,
        tg_username=tg_username,
        buyer_email=buyer_email,
        source="telegram",
        address=context.user_data.pop("_co_address", None),
    )
    if not order_id_int:
        await send_or_edit(update, "⚠️ Не удалось создать заказ", kb_services(), parse_mode=ParseMode.HTML)
        return STATE_CART
    order_id = str(order_id_int)  # внутренний id заказа в БД
    tinkoff_order_id = build_tinkoff_order_id("order", order_id)

    # Резервируем бонусные баллы (если выбрали списание)
    bonus_points = int(context.user_data.get("_co_bonus_points") or 0)
    bonus_discount = float(context.user_data.get("_co_bonus_discount") or 0)
    bonus_reservation_key = None
    if bonus_points > 0:
        try:
            from services.checkout_bonus import apply_bonus_discount
            _iid = context.user_data.get("_co_bonus_internal_id")
            if _iid:
                res = await apply_bonus_discount(_iid, order_id_int, bonus_points, total_rub)
                if res.get("ok"):
                    bonus_reservation_key = res.get("reservation_key")
                    bonus_discount = res.get("rub_discount", bonus_discount)
                    context.user_data["_co_bonus_reservation_key"] = bonus_reservation_key
        except Exception:
            logger.exception("bonus reserve failed, proceeding without discount")
            bonus_points = 0
            bonus_discount = 0

    # Сумма к оплате с учётом скидки
    pay_rub = max(1, int(total_rub - bonus_discount)) if bonus_discount > 0 else total_rub

    # Очищаем checkout-bonus state
    for k in ("_co_bonus_decided", "_co_bonus_info", "_co_bonus_iid",
              "_co_bonus_points", "_co_bonus_discount", "_co_bonus_internal_id"):
        context.user_data.pop(k, None)

    # 2) сохраняем строки заказа (и бронируем коды/внешние резервы ДО выставления счёта)
    try:
        order_items = await _cart_to_order_items_reserved(cart, uid, tester_mode=is_tester_checkout)
        await save_order_items(order_id, order_items)
    except Exception as e:
        logger.exception("reserve/save order_items failed: %s", e)
        await set_order_status(order_id, "CANCELLED", payment_info=f"reserve_failed: {e}")
        try:
            await rollback_order_reservations(order_id)
        except Exception:
            logger.exception("rollback_order_reservations failed")

        context.user_data["cart"] = []

        await send_or_edit(
            update,
            "❌ Не удалось зарезервировать товар (скорее всего, он закончился).\nПожалуйста, вернитесь в магазин и выберите другой вариант.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к магазину", callback_data="buy")],
                [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
            ]),
            parse_mode=ParseMode.HTML,
            context=context
        )
        return STATE_CART

    # meta — только для совместимости (в orders больше не хранится)
    cart_titles = [str(it.get("name") or it.get("title") or it.get("item_title") or "Товар") for it in cart]
    if len(cart_titles) == 1:
        await set_order_item_meta(order_id, str(cart[0].get("sku") or "single"), cart_titles[0])
    else:
        await set_order_item_meta(order_id, "multi", " + ".join(cart_titles))

    if is_tester_checkout:
        fake_resp = {
            "Success": True,
            "Status": "CONFIRMED",
            "PaymentId": f"TEST_TG_{order_id}",
            "Message": "tester_stub_auto_confirm",
            "Amount": int(pay_rub),
        }
        await set_order_payment_id(order_id, str(fake_resp["PaymentId"]))
        context.user_data["last_order_id"] = order_id
        context.user_data["last_payment_id"] = str(fake_resp["PaymentId"])
        context.user_data.pop("last_payment_url", None)

        await confirm_order_paid(order_id, uid, payment_info=json.dumps(fake_resp, ensure_ascii=False))

        if bonus_reservation_key:
            try:
                from services.checkout_bonus import finalize_bonus_discount
                await finalize_bonus_discount(bonus_reservation_key)
            except Exception:
                logger.exception("finalize_bonus_discount failed for tester order %s", order_id)

        try:
            await process_external_ofd_after_payment(order_id, uid)
        except Exception as e:
            logger.exception("process_external_ofd_after_payment failed for tester order %s: %s", order_id, e)

        codes = await issue_code_for_order(order_id, uid)
        items = await get_order_items(order_id)
        order_row = await get_order(order_id) or {}
        detailed_codes = await get_order_codes_detailed(order_id)

        try:
            user_data = {
                "first_name": user.get("first_name", ""),
                "last_name": user.get("last_name", ""),
                "phone": user.get("phone", ""),
                "email": user.get("email", ""),
                "inn": user.get("inn", ""),
                "city": user.get("city", ""),
                "tg_user_id": uid,
                "tg_username": user.get("tg_username", ""),
            }
            await push_sale_to_crm(
                order_id=order_id,
                total_rub=order_row.get("amount", 0),
                cart=[],
                source="telegram",
                user_data=user_data,
                order_data=order_row,
                items=items,
                codes=codes,
                detailed_codes=detailed_codes,
            )
        except Exception as e:
            logger.exception("push_sale_to_crm failed for tester order %s: %s", order_id, e)

        text = await _build_payment_success_text(
            context, order_row, items, codes, update.effective_user.first_name, detailed_codes=detailed_codes
        )

        try:
            ud = context.application.user_data.get(int(uid), {})
            if isinstance(ud, dict):
                ud["cart"] = []
            context.user_data["cart"] = []
        except Exception:
            logger.exception("failed to clear cart for tester order %s", order_id)

        await send_or_edit(
            update,
            text + "\n\n🧪 Тестовая покупка подтверждена автоматически без банка.",
            order_success_kb(),
            parse_mode=ParseMode.HTML,
            context=context,
        )
        return STATE_IDLE

    # 3) строим Receipt под Tinkoff (в копейках)
    items_kop = _make_items_from_cart(cart)              # Name/Price/Quantity/Amount/...
    receipt = build_receipt(user, items_kop)             # добавит Payments/E-mail/Phone/...

    # 4) создаём оплату в Tinkoff (init_payment синхронный -> в поток)
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
        await send_or_edit(update, f"⚠️ Не удалось создать платёж: {e}", kb_services(), parse_mode=ParseMode.HTML)
        return STATE_CART



    # resp формата Tinkoff API: PaymentURL/PaymentId/Status/Success/Message/Details...
    if not resp or resp.get("Success") is False:
        msg = resp.get("Message") or resp.get("Details") or str(resp)
        await set_order_status(order_id, "CANCELLED", payment_info=f"tinkoff_init_error: {msg}")
        try:
            await rollback_order_reservations(order_id)
        except Exception:
            logger.exception("rollback_order_reservations failed")
        await send_or_edit(update, f"⚠️ Ошибка Tinkoff Init: {msg}", kb_services(), parse_mode=ParseMode.HTML)
        return STATE_CART
    
    payment_url = resp.get("PaymentURL") or resp.get("PaymentUrl") or ""
    payment_id = resp.get("PaymentId") or resp.get("PaymentID") or ""
    
    if not payment_url:
        await set_order_status(order_id, "CANCELLED", payment_info=f"tinkoff_no_url: {resp}")
        try:
            await rollback_order_reservations(order_id)
        except Exception:
            logger.exception("rollback_order_reservations failed")
        await send_or_edit(update, f"⚠️ Платёж создан без ссылки: {resp}", kb_services(), parse_mode=ParseMode.HTML)
        return STATE_CART

    # сохраняем payment_id в БД + в контекст
    if payment_id:
        await set_order_payment_id(order_id, str(payment_id))
    
    context.user_data["last_order_id"] = order_id
    context.user_data["last_payment_id"] = str(payment_id) if payment_id else ""
    context.user_data["last_payment_url"] = payment_url
    
    # 5) авто-проверка 30 минут (если у тебя auto_check_payment_job уже поправлен)
    started_at = int(time.time())
    payment_job = context.job_queue.run_repeating(
        auto_check_payment_job,
        interval=30,
        first=30,
        data={"user_id": uid, "user_first_name": update.effective_user.first_name, "order_id": order_id, "payment_id": str(payment_id), "started_at": started_at},
        name=f"auto_check_{order_id}",
    )
    # so we can cancel it after successful manual check
    context.user_data["payment_job"] = payment_job
    
    bonus_line = ""
    if bonus_discount > 0:
        bonus_line = f"Скидка баллами: <b>−{bonus_discount:.0f} ₽</b> ({bonus_points} балл.)\n"
        context.user_data["_co_bonus_reservation_key"] = bonus_reservation_key

    text = (
        "Счёт сформирован ✅\n\n"
        f"Сумма: <b>{total_rub} ₽</b>\n"
        f"{bonus_line}"
        f"К оплате: <b>{pay_rub} ₽</b>\n"
        f"Заказ: <code>{order_id}</code>\n\n"
        "Нажмите «Оплатить», затем «Проверить оплату»."
    )
    await send_or_edit(update, text, pending_payment_kb(context, uid, payment_url, show_retry_btn=False), parse_mode=ParseMode.HTML)
    return STATE_CART




async def handle_co_bonus_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь выбрал списать баллы при оплате."""
    q = update.callback_query
    if q:
        await q.answer()

    info = context.user_data.get("_co_bonus_info") or {}
    iid = context.user_data.get("_co_bonus_iid")
    usable = info.get("usable_points", 0)
    discount = info.get("usable_rub_discount", 0)

    context.user_data["_co_bonus_decided"] = True
    context.user_data["_co_bonus_points"] = usable
    context.user_data["_co_bonus_discount"] = discount
    context.user_data["_co_bonus_internal_id"] = iid

    return await checkout(update, context)


async def handle_co_bonus_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь отказался от списания баллов."""
    q = update.callback_query
    if q:
        await q.answer()

    context.user_data["_co_bonus_decided"] = True
    context.user_data["_co_bonus_points"] = 0
    context.user_data["_co_bonus_discount"] = 0

    return await checkout(update, context)


def pending_payment_kb(context, user_id, pay_url: str | None, show_retry_btn: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if pay_url:
        rows.append([InlineKeyboardButton("💳 Оплатить в Tinkoff", url=pay_url)])
    
    order_id = (context.user_data or {}).get("last_order_id")  # ✅ было order_id
    check_cb = f"pay_check:{order_id}" if order_id else "check_payment"
    rows.append([InlineKeyboardButton("✅ Проверить оплату", callback_data=check_cb)])
    
    if show_retry_btn:
        rows.append([InlineKeyboardButton("🔁 Создать новый платёж", callback_data="checkout")])
    
    return InlineKeyboardMarkup(rows)

async def get_payment_state_async(payment_id: str):
    return await _run_in_thread(get_payment_state, payment_id=payment_id)


# ===== External OFD: покупка кодов после оплаты (идемпотентно) =====
_EXT_OFD_ORDER_LOCKS: dict[str, asyncio.Lock] = {}

def _get_ext_ofd_lock(order_id: str) -> asyncio.Lock:
    lock = _EXT_OFD_ORDER_LOCKS.get(order_id)
    if lock is None:
        lock = asyncio.Lock()
        _EXT_OFD_ORDER_LOCKS[order_id] = lock
    return lock

def _parse_ext_ofd_ids(item_type: str) -> tuple[str, str, str | None] | None:
    s = (item_type or "").lower().strip()
    if not s.startswith("ofd_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    ext_order_id = parts[3] if len(parts) >= 4 and parts[3] else None
    return parts[1], parts[2], ext_order_id


def _parse_ext_service_ids(item_type: str) -> tuple[str, str, str | None] | None:
    s = (item_type or "").lower().strip()
    if not s.startswith("service_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    ext_order_id = parts[3] if len(parts) >= 4 and parts[3] else None
    return parts[1], parts[2], ext_order_id


def _parse_ext_service_order_ids(item_type: str) -> tuple[str, str, str | None] | None:
    s = (item_type or "").lower().strip()
    if not s.startswith("service_ord_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    ext_order_id = parts[3] if len(parts) >= 4 and parts[3] else None
    return parts[1], parts[2], ext_order_id


def _parse_ext_edo_ids(item_type: str) -> tuple[str, str, str | None] | None:
    s = (item_type or "").lower().strip()
    if not s.startswith("edo_ext:"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    ext_order_id = parts[3] if len(parts) >= 4 and parts[3] else None
    return parts[1], parts[2], ext_order_id


def _parse_ext_any_ids(item_type: str) -> tuple[str, str, str, str | None] | None:
    """Возвращает (prefix, operator_id, type_id, ext_order_id)"""
    for prefix, parser in (
        ("ofd_ext", _parse_ext_ofd_ids),
        ("service_ext", _parse_ext_service_ids),
        ("service_ord_ext", _parse_ext_service_order_ids),
        ("edo_ext", _parse_ext_edo_ids),
    ):
        ids = parser(item_type)
        if ids:
            op, tp, oid = ids
            return prefix, op, tp, oid
    return None

def _find_ext_ofd_product(products: dict, operator_id: str, type_id: str):
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
                if int(item.get("operatorId")) == op and int(item.get("typeId")) == tp:
                    return str(vendor), item
            except Exception:
                continue
    return None, None


def _cart_to_order_items(cart: list[dict]) -> list[dict]:
    """
    Преобразует корзину в строки заказа.
    """
    out: list[dict] = []

    for it in (cart or []):
        sku = str(it.get("sku") or "").strip()
        name = str(it.get("name") or it.get("title") or sku).strip()
        price = int(it.get("price") or 0)
        qty = int(it.get("qty") or 1)
        meta = it.get("meta") or {}
        
        if qty < 1:
            qty = 1

        for _ in range(qty):
            out.append({
                "sku": sku,
                "name": name,
                "price": price,
                "qty": 1,
                "meta": meta,
            })

    return out



async def process_external_ofd_after_payment(order_id: str, user_id: str) -> None:
    """После оплаты:
    - для внешних товаров (ofd_other/service/edo) оплачиваем ранее созданные резервы на платформе
      и записываем реальные code_value в уже забронированные строки `codes`.

    Идемпотентно: повторный вызов не оплатит/не перезапишет уже выданные коды.
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

        # Обновляем кэши для vendor/source_price/months
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
                # оплачиваем резерв на платформе и получаем код
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

async def auto_check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    # python-telegram-bot v20+: job data is stored on the Job instance (context.job),
    # not on JobQueue (context.job_queue).
    job = getattr(context, "job", None)
    data = (getattr(job, "data", None) if job else None) or {}

    uid = str(data.get("user_id") or "")
    order_id = data.get("order_id")
    payment_id = data.get("payment_id")
    started_at = float(data.get("started_at") or time.time())
    first_name = str(data.get("user_first_name") or "")

    # стоп через PAYMENT_WAIT_TIMEOUT_SEC
    if time.time() - started_at > PAYMENT_WAIT_TIMEOUT_SEC:
        try:
            await set_order_status(str(order_id), "CANCELLED", payment_info="payment_timeout")
            await rollback_order_reservations(str(order_id))
        except Exception:
            logger.exception("timeout cancel/rollback failed")
        job.schedule_removal()
        return

    if not uid or not order_id or not payment_id:
        job.schedule_removal()
        return

    # ✅ get_payment_state принимает ТОЛЬКО именованный аргумент payment_id
    resp = await _run_in_thread(get_payment_state, payment_id=str(payment_id))

    # если Tinkoff вернул ошибку/неуспех — просто ждём следующий тик
    if not resp or resp.get("Success") is False:
        return

    status = str(resp.get("Status") or "").upper()
    if status not in ("CONFIRMED", "AUTHORIZED"):
        return

    # подтверждаем оплату
    await confirm_order_paid(order_id, uid, payment_info=json.dumps(resp, ensure_ascii=False))

    # Финализация бонусной скидки (если есть)
    _rkey = (data or {}).get("_co_bonus_reservation_key") or ""
    if not _rkey and job and hasattr(job, 'context') and hasattr(job.context, 'user_data'):
        _rkey = (job.context.user_data or {}).get("_co_bonus_reservation_key", "")
    if _rkey:
        try:
            from services.checkout_bonus import finalize_bonus_discount
            await finalize_bonus_discount(_rkey)
        except Exception:
            logger.exception("finalize_bonus_discount failed for order %s", order_id)

    try:
        await process_external_ofd_after_payment(order_id, uid)
    except Exception as e:
        logger.exception("process_external_ofd_after_payment failed: %s", e)

    # выдача кодов идемпотентна
    codes = await issue_code_for_order(order_id, uid)
    items = await get_order_items(order_id)
    order_row = await get_order(order_id) or {}
    detailed_codes = await get_order_codes_detailed(order_id)
    
    # Push sale notification to Django CRM
    try:
        user = await get_user(uid) or {}
        user_data = {
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "phone": user.get("phone", ""),
            "email": user.get("email", ""),
            "inn": user.get("inn", ""),
            "city": user.get("city", ""),
            "tg_user_id": uid,
            "tg_username": user.get("tg_username", ""),
        }
        await push_sale_to_crm(
            order_id=order_id,
            total_rub=order_row.get("amount", 0),
            cart=[],
            source="telegram",
            user_data=user_data,
            order_data=order_row,
            items=items,
            codes=codes,
            detailed_codes=detailed_codes,
        )
    except Exception as e:
        logger.exception("push_sale_to_crm failed: %s", e)
    
    text = await _build_payment_success_text(context, order_row, items, codes, first_name, detailed_codes=detailed_codes)


    # чистим корзину пользователя
    try:
        ud = context.application.user_data.get(int(uid), {})
        if isinstance(ud, dict):
            ud["cart"] = []

        # дополнительная страховка (на случай другого хранилища user_data)
        try:
            context.user_data["cart"] = []
        except Exception:
            pass

    except Exception as e:
        print(e)
        pass

    # Удаляем сообщение с кнопками "Оплатить" / "Проверить оплату"
    from message_cleanup import safe_delete_message_tg, TG_LAST_MENU_KEY
    try:
        ud = context.application.user_data.get(int(uid), {})
        pay_screen_mid = (ud or {}).get(TG_LAST_MENU_KEY)
        if pay_screen_mid:
            await safe_delete_message_tg(context, int(uid), int(pay_screen_mid))
            ud.pop(TG_LAST_MENU_KEY, None)
    except Exception:
        logger.exception("auto_check: failed to delete payment screen for uid=%s", uid)

    await context.bot.send_message(
        chat_id=int(uid),
        text=text,
        reply_markup=order_success_kb(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    job.schedule_removal()



async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    uid = str(q.from_user.id)
    data = (q.data or "").strip()

    # order_id берём из callback pay_check:<order_id> или из контекста
    if data.startswith("pay_check:"):
        order_id = data.split("pay_check:", 1)[1].strip()
    else:
        order_id = (context.user_data or {}).get("last_order_id") or ""

    if not order_id:
        await q.edit_message_text(
            "Не нашёл заказ для проверки оплаты. Оформите заказ заново.",
            reply_markup=kb_services(),
        )
        return STATE_IDLE

    order = await get_order(order_id) or {}
    payment_id = (order.get("payment_id") or (context.user_data or {}).get("last_payment_id") or "").strip()

    if not payment_id:
        await q.edit_message_text(
            "У этого заказа нет payment_id. Создайте платёж заново.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Создать платёж", callback_data="checkout")]]),
        )
        return STATE_CART

    # GetState синхронный -> в поток
    try:
        resp = await _run_in_thread(get_payment_state, payment_id=str(payment_id))
    except Exception as e:
        logger.exception("Tinkoff get_payment_state failed")
        await q.edit_message_text(
            f"⚠️ Не удалось проверить оплату: {e}",
            reply_markup=pending_payment_kb(context, uid, (context.user_data or {}).get("last_payment_url"), show_retry_btn=True),
        )
        return STATE_CART

    if not resp or resp.get("Success") is False:
        msg = resp.get("Message") or resp.get("Details") or str(resp)
        await q.edit_message_text(
            f"⚠️ Ошибка Tinkoff GetState: {msg}",
            reply_markup=pending_payment_kb(context, uid, (context.user_data or {}).get("last_payment_url"), show_retry_btn=True),
        )
        return STATE_CART

    status = str(resp.get("Status") or "").upper()

    # оплачено?
    if status not in ("CONFIRMED", "AUTHORIZED"):
        await q.edit_message_text(
            f"Оплата пока не подтверждена.\nСтатус: <b>{status or '—'}</b>\n\n"
            "Если вы уже оплатили — подождите 10–30 секунд и нажмите «Проверить оплату» снова.",
            parse_mode=ParseMode.HTML,
            reply_markup=pending_payment_kb(context, uid, (context.user_data or {}).get("last_payment_url"), show_retry_btn=True),
        )
        return STATE_CART

    # фиксируем оплату в БД (идемпотентно)
    await confirm_order_paid(order_id, uid, payment_info=json.dumps(resp, ensure_ascii=False))

    # Финализация бонусной скидки
    _rk = (context.user_data or {}).pop("_co_bonus_reservation_key", "")
    if _rk:
        try:
            from services.checkout_bonus import finalize_bonus_discount
            await finalize_bonus_discount(_rk)
        except Exception:
            logger.exception("finalize_bonus_discount failed for order %s", order_id)

    try:
        await process_external_ofd_after_payment(order_id, uid)
    except Exception as e:
        logger.exception("process_external_ofd_after_payment failed: %s", e)

    # stop auto-check job (it is a Job instance saved during checkout)
    pj = (context.user_data or {}).pop("payment_job", None)
    if pj:
        try:
            pj.schedule_removal()
        except Exception:
            logger.exception("failed to schedule_removal for payment_job")
    # выдача кодов идемпотентна и строго по order_items.requires_code=1
    codes = await issue_code_for_order(order_id, uid)           # {'ofd_first': [...], 'ofd_platform': [...], ...}
    items = await get_order_items(order_id)
    order_row = await get_order(order_id) or order
    
    detailed_codes = await get_order_codes_detailed(order_id)
    
    # Push sale notification to Django CRM
    try:
        user = await get_user(uid) or {}
        user_data = {
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "phone": user.get("phone", ""),
            "email": user.get("email", ""),
            "inn": user.get("inn", ""),
            "city": user.get("city", ""),
            "tg_user_id": uid,
            "tg_username": user.get("tg_username", ""),
        }
        await push_sale_to_crm(
            order_id=order_id,
            total_rub=order_row.get("amount", 0),
            cart=[],
            source="telegram",
            user_data=user_data,
            order_data=order_row,
            items=items,
            codes=codes,
            detailed_codes=detailed_codes,
        )
    except Exception as e:
        logger.exception("push_sale_to_crm failed: %s", e)
    
    text = await _build_payment_success_text(context, order_row, items, codes, update.effective_user.first_name, detailed_codes=detailed_codes)


    # корзину чистим после подтверждённой оплаты
    try:
        ud = context.application.user_data.get(int(uid), {})
        if isinstance(ud, dict):
            ud["cart"] = []

        # дополнительная страховка (на случай другого хранилища user_data)
        try:
            context.user_data["cart"] = []
        except Exception:
            pass

    except Exception as e:
        print(e)
        pass

    await q.edit_message_text(
        text,
        reply_markup=order_success_kb(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return STATE_IDLE


# ===== Админка =====
# Перенести сюда:
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return STATE_IDLE

    kb = [
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🧾 Последние выдачи", callback_data="admin_recent")],
        # [InlineKeyboardButton("➕ Загрузить коды (текстом)", callback_data="admin_add_codes")],
        [InlineKeyboardButton("🧪 Выдать код БЕЗ оплаты", callback_data="admin_give_code")],
        [InlineKeyboardButton("🔔 Разослать напоминания всем (тест)", callback_data="admin_ofd_broadcast_all")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="back_to_main")],
    ]

    rp = InlineKeyboardMarkup(kb)

    if update.callback_query:
        await update.callback_query.edit_message_text("🛠 *Админка*", reply_markup=rp, parse_mode="Markdown")
    else:
        await update.message.reply_text("🛠 *Админка*", reply_markup=rp, parse_mode="Markdown")
    return STATE_IDLE

async def admin_show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return STATE_IDLE
    codes = await get_stats()
    orders = await get_orders_stats()
    text = (
        "📊 *Статистика*\n\n"
        f"Коды: всего {codes['total']}, свободно {codes['free']}, выдано {codes['used']}\n"
        f"Заказы: всего {orders['total']}, подтверждено {orders['confirmed']}, в работе {orders['init']}, прочее {orders['other']}"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]])
    )
    return STATE_IDLE

async def admin_show_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return STATE_IDLE

    rows = await get_recent_issues(limit=20)
    if not rows:
        text = "Пока нет выданных кодов."
    else:
        lines = []
        for r in rows:
            name = r.get("buyer_name") or "—"
            phone = r.get("buyer_phone") or "—"
            uname = ("@" + r["tg_username"]) if r.get("tg_username") else "—"
            lines.append(
                f"• `{r['code']}` — {name} ({phone}), {uname} — user_id={r['user_id']} — {r['issued_at']} — order={r['order_id']}"
            )
        text = "🧾 *Последние выдачи*\n\n" + "\n".join(lines)

    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]])
    )
    return STATE_IDLE

async def admin_add_codes_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return STATE_IDLE
    await update.callback_query.edit_message_text(
        "Вставьте коды текстом, *по одному в строке*.\n\nНапример:\n`ABCD-1111`\n`ABCD-2222`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="admin_menu")]])
    )
    return STATE_ADMIN_ADD_CODES

async def admin_add_codes_receive(update, context):
    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("Пришлите коды текстом, каждый с новой строки.")
        return STATE_ADMIN_ADD_CODES

    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    code_type = "ofd_first"

    # необязательный заголовок
    if lines and lines[0].lower() in ("type=ofd_platform", "ofd_platform", "platform"):
        code_type = "ofd_platform"
        lines = lines[1:]

    added = await add_codes_bulk(lines, code_type=code_type)

    await update.message.reply_text(f"Добавлено кодов ({code_type}): {added}")
    return STATE_IDLE



async def admin_give_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return STATE_IDLE

    q = update.callback_query
    await q.answer()

    uid = str(update.effective_user.id)
    username = (update.effective_user.username or "")

    order_id_int = await create_order(
        user_id=uid,
        amount=0,
        buyer_name="ADMIN TEST",
        buyer_phone=None,
        tg_username=username,
        source="telegram",
    )
    if not order_id_int:
        await q.edit_message_text("⚠️ Не удалось создать заказ")
        return STATE_IDLE
    order_id = str(order_id_int)

    # создаём строку заказа + сразу бронируем код из пула
    code_id = await reserve_pool_code_for_user(uid, "ofd_first", sell_price=0, months_number=12)
    if not code_id:
        await q.edit_message_text("❌ Нет свободных кодов в пуле ОФД.")
        return STATE_IDLE

    await save_order_items(order_id, [{
        "code_id": int(code_id),
        "qty": 1,
        "price": 0,
    }])

    await confirm_order_paid(order_id, uid, payment_info="admin_manual_issue")

    codes = await issue_code_for_order(order_id=order_id, user_id=uid)
    ofd_codes = codes.get("ofd_first") or []

    if not ofd_codes:
        await q.edit_message_text(
            "😔 Свободных кодов ОФД нет.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]])
        )
        return STATE_IDLE

    await q.edit_message_text(
        f"🔑 Код ОФД выдан: `{ofd_codes[0]}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]])
    )
    return STATE_IDLE

async def ofd_reminder_broadcast_all(app):
    """Тестовая рассылка: шлём всем, у кого есть user_id и слоты, без фильтра по дням."""
    today = datetime.date.today()
    sent = 0
    skipped_no_user = 0
    errors = 0

    for client in await get_all_clients_with_ofd():
        user_id = client.get("user_id")
        if not user_id:
            skipped_no_user += 1
            continue
        inn = client.get("inn")
        slots = client.get("ofd_slots") or []
        for s in slots:
            exp_str = s.get("ofd_expires_at")
            days_left = None
            date_obj = None
            try:
                date_obj = datetime.date.fromisoformat(exp_str)
                days_left = (date_obj - today).days
            except Exception as e:
                print(e)
                pass

            text_lines = [
                "🧪 <b>Тест напоминания (проверка доставки)</b>",
                f"ККТ: <b>{s.get('kkt_slot')}</b>",
                f"ИНН: <code>{inn}</code>",
            ]
            if date_obj:
                text_lines.append(f"Дата окончания: <b>{date_obj:%d.%m.%Y}</b>")
            else:
                text_lines.append(f"Дата окончания: <code>{exp_str or '—'}</code>")
            if days_left is not None:
                text_lines.append(f"До конца: <b>{days_left}</b> дн.")

            try:
                await app.bot.send_message(
                    chat_id=user_id,
                    text="\n".join(text_lines),
                    parse_mode=ParseMode.HTML
                )
                sent += 1
            except Exception as e:
                errors += 1
                logger.warning("[ofd_broadcast_all] failed to notify %s: %s", user_id, e)

            # легкий троттлинг, чтобы не упереться в лимиты Telegram
            await asyncio.sleep(0.04)

    logger.info("[ofd_broadcast_all] sent=%d, skipped_no_user=%d, errors=%d",
                sent, skipped_no_user, errors)
    return sent, skipped_no_user, errors

def kb_services() -> InlineKeyboardMarkup:
    # Универсальная "куда дальше"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧰 Услуги", callback_data="shop")],
        [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])


def kb_cart_empty() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧰 Услуги", callback_data="shop")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])



def kb_check_payment(order_id: str) -> InlineKeyboardMarkup:
    # В bot_main.py хэндлер ждёт callback вида pay_check:<order_id>
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"pay_check:{order_id}")],
        [InlineKeyboardButton("🧰 Услуги", callback_data="shop")],
        [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])


async def admin_ofd_broadcast_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return STATE_IDLE

    q = update.callback_query
    await q.answer()
    back_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]]
    )
    await q.edit_message_text(
        "⏱ Запускаю тестовую рассылку всем с ОФД-слотами…",
        reply_markup=back_kb,
    )

    sent, skipped, errors = await ofd_reminder_broadcast_all(context.application)

    await q.edit_message_text(
        f"Готово.\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"🚫 Без user_id: <b>{skipped}</b>\n"
        f"⚠️ Ошибок: <b>{errors}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb,
    )
    return STATE_IDLE

async def show_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()

    text = "🧰 <b>Услуги</b>\n\nВыберите раздел:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏛 ЧЗ/ТС Пиот/ЕГАИС/ЭДО", callback_data="usl_gis")],
        [InlineKeyboardButton("Подписать накладные // УПД", callback_data="usl_leaf:upd_sign")],
        [InlineKeyboardButton("🎁 Акции и скидки", callback_data="usl_newbiz")],
        [InlineKeyboardButton("📜 Регистрация / Замена / Обновления", callback_data="usl_reg")],
        [InlineKeyboardButton("📄 Сертификаты / Доп. декларации", callback_data="usl_leaf:cert")],
        [InlineKeyboardButton("🧻 Бумага", callback_data="shop_paper")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])

    if q:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return STATE_IDLE

async def usl_gis_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ТС Пиот", callback_data="usl_gis_sys:tspiot")],
        [InlineKeyboardButton("Честный знак", callback_data="usl_gis_sys:chz")],
        [InlineKeyboardButton("ЕГАИС", callback_data="usl_gis_sys:egais")],
        [InlineKeyboardButton("ЭДО", callback_data="usl_gis_sys:edo")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="shop")],
    ])
    await q.edit_message_text("🏛 <b>Гос. инф. системы</b>\n\nВыберите систему:", parse_mode=ParseMode.HTML, reply_markup=kb)
    return STATE_IDLE

GIS_LABELS = {
    "tspiot": "ТС Пиот",
    "chz": "Честный знак",
    "egais": "ЕГАИС",
    "edo": "ЭДО",
}

async def usl_gis_sys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    sys_code = q.data.split(":", 1)[1]
    sys_name = GIS_LABELS.get(sys_code, sys_code)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Регистрация", callback_data=f"usl_leaf:gis:{sys_code}:reg")],
        [InlineKeyboardButton("Настройка", callback_data=f"usl_leaf:gis:{sys_code}:setup")],
        [InlineKeyboardButton("Аудит / проверка работы системы", callback_data=f"usl_leaf:gis:{sys_code}:audit")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="usl_gis")],
    ])
    await q.edit_message_text(f"🏛 <b>{sys_name}</b>\n\nВыберите услугу:", parse_mode=ParseMode.HTML, reply_markup=kb)
    return STATE_IDLE

async def usl_reg_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Замена ФН", callback_data="usl_leaf:reg:fn_replace")],
        [InlineKeyboardButton("Продление ОФД", callback_data="usl_leaf:reg:ofd_extend")],
        [InlineKeyboardButton("Регистрация ККТ", callback_data="usl_leaf:reg:kkt_reg")],
        [InlineKeyboardButton("Обновления", callback_data="usl_leaf:reg:updates")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="shop")],
    ])
    await q.edit_message_text("🧾 <b>Регистрация / Замена / Обновления / Продление</b>\n\nВыберите пункт:",
                              parse_mode=ParseMode.HTML, reply_markup=kb)
    return STATE_IDLE

async def usl_newbiz_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Подбор выгодного решения", callback_data="usl_leaf:newbiz:best_solution")],
        [InlineKeyboardButton("Подбор оборудования", callback_data="usl_leaf:newbiz:equipment")],
        [InlineKeyboardButton("Открытие ИП", callback_data="usl_leaf:newbiz:open_ip")],
        [InlineKeyboardButton("🎁 Акции", callback_data="usl_leaf:newbiz:kassa10000")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="shop")],
    ])
    await q.edit_message_text("🎁 <b>Акции и скидки</b>\n\nВыберите пункт:", parse_mode=ParseMode.HTML, reply_markup=kb)
    return STATE_IDLE

async def usl_leaf_stub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    # пример data:
    # usl_leaf:gis:tspiot:setup
    # usl_leaf:reg:fn_replace
    parts = q.data.split(":")
    # сохраним выбор на будущее (когда будем создавать заявку директору)
    context.user_data["pending_service"] = q.data

    text = (
        "✍️ <b>Текст сообщения бота</b>\n\n"
        "По возможности максимально подробно изложите суть проблемы, с которой вы столкнулись.\n\n"
        "Пример: касса Эвотор 7, показывает ошибку №24, не даёт принять накладные.\n\n"
        "—\n"
        "Пока это тестовый шаг: далее подключим создание заявки директору и перевод диалога."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="shop")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return STATE_IDLE
