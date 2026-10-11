# bot_main.py

import logging
from pathlib import Path
from datetime import time as dtime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / "inf.env")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    PicklePersistence,
    filters,
)
from bot_services import (
    svc_root,
    svc_gis,
    svc_gis_sys,
    svc_reg,
    svc_newbiz,
    svc_leaf,
)
from bot_alfabank_ip import (
    svc_ip_entry,
    ip_wiz_confirm,
    ip_wiz_change,
    ip_wiz_cancel,
    ip_wiz_city_retry,
    ip_save_field,
    ip_submit,
    ip_refresh_status,
    _PENDING_EDIT_KEY as _IP_PENDING_EDIT_KEY,
)
from notice_center import NoticeKind, upsert_notice_tg
import asyncio
import time

import os
import sys
import re

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
from db import (
    get_all_user_ids,
    get_all_clients_with_ofd,
    get_client_by_inn,
    get_client_fn_slots,
    get_client_mark_slots,
    get_user,
    get_specialist_by_tg,
    get_specialist_by_max,
    list_ready_specialists_for_notify,
    list_pending_chat_ids_for_profession,
    is_partner_specialist,
    list_codes_expiring_by_expires_at,
    purge_expired_codes_older_than_months,
    log_event, apply_referral_code

)
from telegram.ext import ApplicationHandlerStop

from bot_registration import (
    # старт / идентификация / регистрация
    start,
    ident_start,
    start_registration,
    on_name,
    on_contact,
    on_text_inn,
    skip_inn_cb,
    cb_consent,
    cb_ident,
    show_referral_qr,

    # профиль / коды / ОФД
    show_profile,
    show_my_codes,
    show_profile_ofd,
    edit_profile_menu,
    profile_finish_verify,
    extra_verify_start,
    show_loyalty_history,
    show_referral_progress,

    # согласие
    show_consent,

    # редактирование профиля
    edit_firstname_prompt,
    edit_lastname_prompt,
    edit_phone_prompt,
    edit_email_prompt,
    edit_inn_prompt,
    edit_city_prompt,
    save_firstname,
    save_lastname,
    save_phone_from_contact,
    reject_manual_phone_in_edit,
    save_email,
    save_inn,
    save_city,
    save_kkt_factory_number,
    save_address,
    handle_co_addr_use,
    handle_co_addr_new,

    # анкета / Django CRM
    application_form_start,
    form_tp,
    form_addr,
    form_act_cb,
    form_assort,
    form_services_cb,
    form_sno_cb,
    form_marked_cb,
    form_fnterm_cb,
    form_ofd_cb,
    form_banks,

    # навигация / сброс / fallback
    back_to_main,
    cancel,
    text_fallback,
    reset_me,
    wipe_me,
    delete_me,

    referral_trigger,
    renew_inn,
    renew_kkt_factory_number,
    cb_check_subscription_tg,

)

from bot.bot_agent_chat import (
    agent_entry,
    AG_WAIT_PHONE, AG_MENU_CHATS, AG_CHAT_VIEW, AG_WAIT_REPLY, AG_WAIT_PAY_AMOUNT, AG_CLOSE_CONFIRM,
    on_agent_contact,

    cb_refresh, cb_ref_qr, cb_back_to_chats,
    cb_open_chat as ag_cb_open_chat,

    cb_close, cb_close_yes, cb_close_no, cb_close_cancel,
    cb_offer_accept, cb_offer_decline, cb_dir_prof, cb_dir_sections, _render_chat_list,
    cb_reassign_chat,cb_reassign_to
)

from bot_client_chat import (
    client_chat_start,
    CC_WAIT_TYPE, CC_WAIT_CATEGORY, CC_WAIT_PROMO_CHOICE, CC_WAIT_PROMO_INPUT, CC_WAIT_CONTENT, CC_WAIT_REPLY,
    cb_type, cb_sub, cb_category, cb_promo_yes, cb_promo_no, on_promo_input, on_content_message,
    cb_back as cc_cb_back, cb_menu as cc_cb_menu,
    cb_reply as cc_cb_reply, on_reply_text,
    cb_my_chats, cb_mychat_open,
    cb_close as cc_cb_close, cb_close_no as cc_cb_close_no,
    cb_shop_consult,
    cb_upd_all, cb_upd_selective,
    cb_open_chat as cc_cb_open_chat,
)

from bot_catalog import (
    # магазин / корзина
    show_shop,
    show_services_fn,
    show_services_ofd,
    show_services_service,
    show_services_edo,
    add_to_cart,
    show_cart,
    clear_cart,
    remove_from_cart,
    cart_inc,
    cart_dec,
    cart_noop,

    # услуги (новое)
    usl_gis_menu,
    usl_gis_sys_menu,
    usl_reg_menu,
    usl_newbiz_menu,
    usl_leaf_stub,

    # оплата
    checkout,
    check_payment,

    # админка
    admin_menu,
    admin_show_stats,
    admin_show_recent,
    admin_add_codes_start,
    admin_add_codes_receive,
    admin_give_code,
    admin_ofd_broadcast_all,
    show_shop_equipment,
    show_shop_paper,
    eq_root_cb, eq_category_cb, eq_view_cb, eq_add_cb,
    refresh_ext_ofd_products,
    refresh_ext_service_products,
    refresh_ext_edo_products,
    ext_ofd_provider_cb,
    ext_ofd_add_cb,
    ext_srv_provider_cb,
    ext_srv_add_cb,
    ext_edo_provider_cb,
    ext_edo_add_cb,
    ext_srv_ord_provider_cb,
    ext_srv_ord_add_cb,
    handle_co_bonus_use,
    handle_co_bonus_skip,
)
from bot_orders_management import (
    show_users_with_orders,
    show_orders_of_user,
    order_menu,
    dir_set_service_code_start,
    dir_set_service_code_receive,
    close_order_confirm,
    close_order_confirm_yes,
    show_my_orders,
    my_order_menu,
)

from telegram.error import BadRequest, Forbidden

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )
refresh_ext_ofd_products(force=True)

_START_LOCKS: dict[int, asyncio.Lock] = {}



def _get_start_lock(uid: int) -> asyncio.Lock:
    lock = _START_LOCKS.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _START_LOCKS[uid] = lock
    return lock


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
TELEGRAM_PROXY  = os.getenv("TELEGRAM_PROXY ", None)

# --- ключи в application.bot_data ---
BD_KEY_PENDING_SNAPSHOT = "pending_snapshot_by_prof"  # {prof: set(chat_id)}
BD_KEY_LAST_PING_TS = "pending_last_ping_ts"  # {chat_id: unix_ts}
BD_KEY_MUTE_UNTIL_TS = "spec_mute_until_ts"  # {tg_user_id: unix_ts} опционально
BD_KEY_BOOT_NOTICE_MSG = "boot_notice_msg_by_uid"  # {tg_user_id: message_id}
BD_KEY_PENDING_RECIPIENTS_FINGERPRINT = "pending_recipients_fingerprint"




def _kb_open_chats_for_specialist(role: str) -> InlineKeyboardMarkup:
    # директору логичнее открывать выбор разделов, обычному спецу — просто список
    cb = "ag_dir_sections" if (role == "director") else "ag_refresh"
    rows = [[InlineKeyboardButton("📂 Открыть заявки", callback_data=cb)]]

    # ✅ Только директор может выходить в клиентское меню
    if role == "director":
        rows.append([InlineKeyboardButton("🔙 В меню клиента", callback_data="back_to_main")])

    return InlineKeyboardMarkup(rows)


# ====== Логирование ======
_log_dir = Path(__file__).resolve().parent.parent / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_tg_log_file = _log_dir / "tg_bot.log"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_tg_log_file, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger(__name__)
logger.info("Telegram bot logging initialized: file=%s", _tg_log_file)

BOOT_NOTICE_TEXT = (
    "Мы обновили бота ✨\n"
    "Если кнопки подвисли — просто нажмите «Обновить меню»."
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
from bot_common import (
    STATE_IDLE,
    STATE_CONSENT,
    STATE_PRODUCT_SELECT,
    STATE_CART,
    STATE_CO_EMAIL,
    STATE_CO_INN,
    STATE_CO_CITY,
    STATE_CO_KKT_FACTORY_NUMBER,
    STATE_CO_BONUS,
    STATE_CO_ADDRESS,
    STATE_DIR_SET_SERVICE_CODE,
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
    send_or_edit,
    nav_kb,
    show_main_menu
)
from bot_common import clear_chat_reply_flags, cleanup_expired_reply_flags, get_armed_reply, is_specialist_user, \
    arm_chat_reply


def refresh_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Обновить меню", callback_data="refresh")]]
    )


async def refresh_ui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    tg_user = update.effective_user
    tg_uid = int(tg_user.id)
    username = (tg_user.username or "").strip()

    # message_id того сообщения, откуда нажали refresh (обычно boot-notice)
    clicked_mid = None
    clicked_chat_id = None
    if q and q.message:
        try:
            clicked_mid = int(q.message.message_id)
            clicked_chat_id = int(q.message.chat_id)
        except Exception:
            clicked_mid = None
            clicked_chat_id = None

    spec = await get_specialist_by_tg(tg_user_id=tg_uid, tg_username=username)

    # 1) обновляем интерфейс
    if spec and not is_partner_specialist(spec) and spec.get("role") != "director":
        # тут оставляем как есть (у спеца может быть свой UI)
        result = await _render_chat_list(update, context)

        # boot-map всё равно чистим, чтобы не держать мусор
        if clicked_mid and clicked_chat_id:
            try:
                bd = context.application.bot_data
                boot_map = bd.get(BD_KEY_BOOT_NOTICE_MSG)
                if isinstance(boot_map, dict) and int(boot_map.get(tg_uid) or 0) == clicked_mid:
                    boot_map.pop(tg_uid, None)
            except Exception:
                pass

        # ВАЖНО: для спецов НЕ удаляем clicked_mid автоматически,
        # потому что _render_chat_list может превратить это сообщение в экран списка чатов.

        return result

    # --- Клиент/директор: делаем идеальный refresh ---
    # 2) показываем меню НОВЫМ сообщением вниз (не редактируем boot-notice!)
    result = await show_main_menu(
        update,
        context,
        "Готово! Меню обновлено.",
        force_send_new=True,  # ✅ главное
    )

    # 3) теперь можно безопасно удалить boot-notice
    if clicked_mid and clicked_chat_id:
        try:
            bd = context.application.bot_data
            boot_map = bd.get(BD_KEY_BOOT_NOTICE_MSG)
            if isinstance(boot_map, dict) and int(boot_map.get(tg_uid) or 0) == clicked_mid:
                boot_map.pop(tg_uid, None)
        except Exception:
            pass

        asyncio.create_task(_safe_delete_msg(context.bot, clicked_chat_id, clicked_mid))

    return result

async def cb_any_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Catch-all роутер для callback_data.
    Срабатывает ТОЛЬКО если предыдущие handlers (conv_handler и т.п.) не поймали callback.
    """
    q = update.callback_query
    if not q:
        return STATE_IDLE

    data = (q.data or "").strip()
    logger.info(
        "TG callback router: callback_data=%s tg_user_id=%s",
        data,
        update.effective_user.id if update.effective_user else None,
    )

    # ⚠️ Важно: здесь лучше НЕ делать q.answer() "всегда",
    # потому что многие твои целевые функции сами делают q.answer().
    # Если ответить дважды — иногда ловится BadRequest.
    async def _safe_answer():
        try:
            await q.answer()
        except Exception:
            pass

    try:
        # 1) Чат-система (агент/клиент) — маршрутизируем по префиксам
        if data.startswith("ag_"):
            # минимум: вернуть спеца в список чатов
            if re.fullmatch(r"ag_refresh", data):
                return await cb_refresh(update, context)
            if re.fullmatch(r"ag_ref_qr(?::(telegram|max))?", data):
                return await cb_ref_qr(update, context)
            if re.fullmatch(r"ag_back_to_chats", data):
                return await cb_back_to_chats(update, context)
            if re.fullmatch(r"ag_dir_sections", data):
                return await cb_dir_sections(update, context)
            if re.fullmatch(r"ag_dir_prof:(all|\d+)", data):
                return await cb_dir_prof(update, context)
            if re.fullmatch(r"ag_chat:\d+", data):
                return await ag_cb_open_chat(update, context)

            if data.startswith("ag_offer_yes:"):
                return await cb_offer_accept(update, context)
            if data.startswith("ag_offer_no:"):
                return await cb_offer_decline(update, context)

            if data.startswith(("ag_close:", "ag_close_yes:", "ag_close_no:", "ag_close_cancel:")):
                if data.startswith("ag_close_yes:"):
                    return await cb_close_yes(update, context)
                if data.startswith("ag_close_no:"):
                    return await cb_close_no(update, context)
                if data.startswith("ag_close_cancel:"):
                    return await cb_close_cancel(update, context)
                return await cb_close(update, context)

            # неизвестная ag_* — просто покажем меню чатов
            return await cb_refresh(update, context)

        if data.startswith("cc_") or data.startswith("client_chat_start:"):
            # клиентские обращения / запуск чатов
            # (эти функции у тебя уже есть в NAV_GLOBAL, но здесь мы вызываем напрямую)
            if re.fullmatch(r"client_chat_start:\d+", data):
                try:
                    await q.answer()
                except Exception:
                    pass
                prof = int(data.split(":")[1])
                if await is_specialist_user(update, context):
                    return await agent_entry(update, context)
                u = await get_user(str(update.effective_user.id)) or {}
                phone_ok = bool((u.get("phone") or "").strip())
                if not phone_ok:
                    await q.edit_message_text(
                        "Связь со специалистом доступна после подтверждения телефона в профиле.",
                        reply_markup=await nav_kb(context, update.effective_user.id),
                    )
                    return STATE_IDLE
                return await client_chat_start(update, context, profession=prof)
            if data == "cc_menu":
                return await cc_cb_menu(update, context)
            if data in {"cc_my_chats", "my_chats"}:
                return await cb_my_chats(update, context)
            if data.startswith("cc_mychat:"):
                return await cb_mychat_open(update, context)
            if data.startswith("cc_sub:"):
                return await cb_sub(update, context)
            if data.startswith("cc_back:"):
                return await cc_cb_back(update, context)
            if data.startswith("cc_reply:"):
                return await cc_cb_reply(update, context)
            if data.startswith("cc_close:"):
                return await cc_cb_close(update, context)
            if data.startswith("cc_close_no:"):
                return await cc_cb_close_no(update, context)

            # неизвестная cc_* — откроем главное меню
            return await show_main_menu(update, context, "Открыл меню.")

        # 2) Основная навигация клиента/директора
        if data == "back_to_main":
            return await back_to_main(update, context)
        if data == "profile":
            return await show_profile(update, context)
        if data == "my_orders":
            return await show_my_orders(update, context)
        if re.fullmatch(r"my_order_menu:\d+", data):
            return await my_order_menu(update, context)
        if re.fullmatch(r"my_codes|codes_prev:\d+|codes_next:\d+", data):
            return await show_my_codes(update, context)
        if data == "profile_ofd":
            return await show_profile_ofd(update, context)
        if data == "edit_profile":
            return await edit_profile_menu(update, context)
        if data == "loyalty_history":
            return await show_loyalty_history(update, context)
        if data == "referral_progress":
            return await show_referral_progress(update, context)
        if data == "my_referral_qr" or data.startswith("my_referral_qr:"):
            return await show_referral_qr(update, context)
        if data == "profile_finish_verify":
            return await profile_finish_verify(update, context)

        # 2.1) Управление заказами директора (может прилететь вне ConversationHandler)
        if data == "users_with_orders":
            return await show_users_with_orders(update, context)
        if re.fullmatch(r"user_with_orders:\d+", data):
            return await show_orders_of_user(update, context)
        if re.fullmatch(r"show_order:\d+", data):
            return await order_menu(update, context)
        if re.fullmatch(r"dir_srv_code:\d+:\d+", data):
            return await dir_set_service_code_start(update, context)
        if re.fullmatch(r"close_order_start:\d+", data):
            return await close_order_confirm(update, context)
        if re.fullmatch(r"close_order_finish:\d+", data):
            return await close_order_confirm_yes(update, context)

        if data == "services":
            return await svc_root(update, context)
        if data == "svc_root":
            return await svc_root(update, context)
        if data == "svc_gis":
            return await svc_gis(update, context)
        if data == "svc_reg":
            return await svc_reg(update, context)
        if data == "svc_newbiz":
            return await svc_newbiz(update, context)
        if data.startswith("svc_gis_sys:"):
            return await svc_gis_sys(update, context)
        if data.startswith("svc_leaf:"):
            return await svc_leaf(update, context)
        if data == "svc_ip":
            return await svc_ip_entry(update, context)
        if data == "ip_wiz_confirm":
            return await ip_wiz_confirm(update, context)
        if data == "ip_wiz_change":
            return await ip_wiz_change(update, context)
        if data == "ip_wiz_cancel":
            return await ip_wiz_cancel(update, context)
        if data == "ip_wiz_city_retry":
            return await ip_wiz_city_retry(update, context)
        if data == "ip_submit":
            return await ip_submit(update, context)
        if data == "ip_refresh_status":
            return await ip_refresh_status(update, context)

        if data == "buy":
            return await show_shop(update, context)
        if data == "services_fn":
            return await show_services_fn(update, context)
        if data == "services_ofd":
            return await show_services_ofd(update, context)
        if data == "services_service":
            return await show_services_service(update, context)
        if data == "services_edo":
            return await show_services_edo(update, context)
        if data.startswith("product_"):
            return await add_to_cart(update, context)
        if data.startswith("shop_consult:"):
            return await cb_shop_consult(update, context)
        if data == "shop_equipment":
            return await show_shop_equipment(update, context)
        if data == "shop_paper":
            return await show_shop_paper(update, context)
        if re.fullmatch(r"eq_root:\d+", data):
            return await eq_root_cb(update, context)
        if re.fullmatch(r"eq_cat:\d+:\d+", data):
            return await eq_category_cb(update, context)
        if re.fullmatch(r"eq_view:\d+:\d+:\d+", data):
            return await eq_view_cb(update, context)
        if re.fullmatch(r"eq_add:\d+:\d+:\d+", data):
            return await eq_add_cb(update, context)
        if re.fullmatch(r"ext_ofd_p:\d+", data):
            return await ext_ofd_provider_cb(update, context)
        if re.fullmatch(r"ext_ofd_add:\d+:\d+", data):
            return await ext_ofd_add_cb(update, context)
        if re.fullmatch(r"ext_srv_p:\d+", data):
            return await ext_srv_provider_cb(update, context)
        if re.fullmatch(r"ext_srv_add:\d+:\d+", data):
            return await ext_srv_add_cb(update, context)
        if re.fullmatch(r"ext_edo_p:\d+", data):
            return await ext_edo_provider_cb(update, context)
        if re.fullmatch(r"ext_edo_add:\d+:\d+", data):
            return await ext_edo_add_cb(update, context)
        if re.fullmatch(r"ext_srv_ord_p:\d+", data):
            return await ext_srv_ord_provider_cb(update, context)
        if re.fullmatch(r"ext_srv_ord_add:\d+:\d+", data):
            return await ext_srv_ord_add_cb(update, context)
        if data == "usl_gis":
            return await usl_gis_menu(update, context)
        if data == "usl_reg":
            return await usl_reg_menu(update, context)
        if data == "usl_newbiz":
            return await usl_newbiz_menu(update, context)
        if re.fullmatch(r"usl_gis_sys:[a-z_]+", data):
            return await usl_gis_sys_menu(update, context)
        if data.startswith("usl_leaf:"):
            return await usl_leaf_stub(update, context)

        if data == "cart":
            return await show_cart(update, context)
        if data == "clear_cart":
            return await clear_cart(update, context)
        if re.fullmatch(r"del_\d+", data):
            return await remove_from_cart(update, context)
        if re.fullmatch(r"cart_inc:\d+", data):
            return await cart_inc(update, context)
        if re.fullmatch(r"cart_dec:\d+", data):
            return await cart_dec(update, context)
        if data == "cart_noop":
            return await cart_noop(update, context)
        if data == "checkout":
            return await checkout(update, context)
        if re.fullmatch(r"check_payment|pay_check:.+", data):
            return await check_payment(update, context)

        if data == "application_form":
            return await application_form_start(update, context)
        if data == "ident_start":
            return await ident_start(update, context)
        if data == "extra_verify":
            return await extra_verify_start(update, context)
        if data == "register":
            return await start_registration(update, context)
        if data == "input_new_inn":
            return await renew_inn(update, context)
        if data == "input_new_kkt_number":
            return await renew_kkt_factory_number(update, context)
        if data == "edit_firstname":
            return await edit_firstname_prompt(update, context)
        if data == "edit_lastname":
            return await edit_lastname_prompt(update, context)
        if data == "edit_phone":
            return await edit_phone_prompt(update, context)
        if data == "edit_email":
            return await edit_email_prompt(update, context)
        if data == "edit_inn":
            return await edit_inn_prompt(update, context)
        if data == "edit_city":
            return await edit_city_prompt(update, context)

        if data == "admin_menu":
            return await admin_menu(update, context)
        if data == "admin_stats":
            return await admin_show_stats(update, context)
        if data == "admin_recent":
            return await admin_show_recent(update, context)
        if data == "admin_add_codes":
            return await admin_add_codes_start(update, context)
        if data == "admin_give_code":
            return await admin_give_code(update, context)
        if data == "admin_ofd_broadcast_all":
            return await admin_ofd_broadcast_all(update, context)

        if data == "refresh":
            return await refresh_ui(update, context)

        # 3) Фолбек: кнопка устарела/состояние потеряно → возвращаем меню
        await _safe_answer()
        return await show_main_menu(update, context, "Кнопка устарела — открыл меню.")

    except BadRequest as e:
        # например: message is not modified, query is too old и т.п.
        await _safe_answer()
        try:
            logger.debug("cb_any_router BadRequest: %s", e)
        except Exception:
            pass
        # Важно: не перетираем экран главным меню, если кнопка уже была обработана другим handler'ом
        return STATE_IDLE
    except Exception as e:
        await _safe_answer()
        try:
            logger.debug("cb_any_router error: %s", e)
        except Exception:
            pass
        # Аналогично: не перетираем UI, просто игнорируем
        return STATE_IDLE

def _director_price_msg_key(tg_uid: int) -> str:
    return f"director_price_check_msg:{tg_uid}"


async def _send_or_edit_director_price_check(
    context: ContextTypes.DEFAULT_TYPE,
    tg_uid: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
):
    """
    Пытается отредактировать предыдущее "плановое" сообщение (по message_id из bot_data).
    Если нельзя — отправляет новое и обновляет message_id.
    """
    bot = context.application.bot
    key = _director_price_msg_key(tg_uid)

    # bot_data живёт в PTB persistence (если у тебя включён persistence) и переживает рестарты.
    # Если persistence не включён — будет работать в рамках аптайма (после рестарта создаст новое).
    msg_id = context.application.bot_data.get(key)

    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=tg_uid,
                message_id=int(msg_id),
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as e:
            # типичные случаи: "Message to edit not found", "message can't be edited", etc.
            logger.info("[director_price_check] edit failed for %s: %s -> will send new", tg_uid, e)
        except Forbidden as e:
            logger.warning("[director_price_check] forbidden to edit for %s: %s", tg_uid, e)
            return
        except Exception as e:
            logger.warning("[director_price_check] unexpected edit error for %s: %s", tg_uid, e)

    # fallback: отправляем новое
    try:
        sent = await bot.send_message(
            chat_id=tg_uid,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        context.application.bot_data[key] = sent.message_id
    except Forbidden as e:
        logger.warning("[director_price_check] forbidden to send for %s: %s", tg_uid, e)
    except Exception as e:
        logger.warning("[director_price_check] failed to send for %s: %s", tg_uid, e)

async def director_price_check_job_ctx(context: ContextTypes.DEFAULT_TYPE):
    app = context.application

    try:
        directors = await list_directors() or []
    except Exception as e:
        logger.warning("[director_price_check] DB error: %s", e)
        return

    text = (
        "📌 <b>Понедельник: сверка цен</b>\n"
        "Проверьте актуальность цен/позиций (внешние прайсы/услуги/оборудование) "
        "и при необходимости обновите.\n\n"
        "Если всё ок — просто игнорируйте это сообщение ✅"
    )

    kb = _kb_price_check_for_director()

    for d in directors:
        tg_uid = int(d.get("tg_user_id") or 0)
        if not tg_uid:
            continue

        await _send_or_edit_director_price_check(
            context=context,
            tg_uid=tg_uid,
            text=text,
            reply_markup=kb,
        )

async def support_pending_notifier_job(context):
    """
    Каждые 30 сек:
    - смотрим новые pending-обращения по профессиям
    - шлем уведомления специалистам (и директору) если появилось новое
    - дублируем напоминание по тем же pending каждые 5/10 минут, пока они pending
    """
    app = context.application
    bot = app.bot
    bd = app.bot_data

    now = time.time()

    pending_snapshot = bd.get(BD_KEY_PENDING_SNAPSHOT)
    if not isinstance(pending_snapshot, dict):
        pending_snapshot = {}
        bd[BD_KEY_PENDING_SNAPSHOT] = pending_snapshot

    last_ping = bd.get(BD_KEY_LAST_PING_TS)
    if not isinstance(last_ping, dict):
        last_ping = {}
        bd[BD_KEY_LAST_PING_TS] = last_ping

    mute_until = bd.get(BD_KEY_MUTE_UNTIL_TS)
    if not isinstance(mute_until, dict):
        mute_until = {}
        bd[BD_KEY_MUTE_UNTIL_TS] = mute_until

    # 1) спецы, кому можно отправлять
    try:
        specs = await list_ready_specialists_for_notify()
    except Exception as e:
        print("[support_pending_notifier_job] DB error:", e)
        return

    # 2) какие профессии вообще есть у готовых спецов
    profs = sorted({int(s.get("profession") or 0) for s in specs})

    # 3) обновляем pending-очереди по профессиям
    current_by_prof: dict[int, set[int]] = {}
    for prof in profs:
        ids = await list_pending_chat_ids_for_profession(prof)
        current_by_prof[prof] = set(ids)

    # 4) сравниваем с прошлым снимком → определяем, что НОВОЕ появилось
    new_pending_by_prof: dict[int, set[int]] = {}
    for prof, cur_set in current_by_prof.items():
        prev_set = set(pending_snapshot.get(prof) or [])
        new_ids = cur_set - prev_set
        if new_ids:
            new_pending_by_prof[prof] = new_ids
        pending_snapshot[prof] = set(cur_set)

    # 5) уведомления
    # Логика:
    # - если появились новые pending → сразу шлём "Есть новые заявки"
    # - если pending висит долго → напоминаем раз в 10 минут (можешь сделать 5)
    REMIND_EVERY_SEC = 10 * 60
    current_fingerprint = (
        tuple((int(p), tuple(sorted(int(x) for x in ids))) for p, ids in sorted(current_by_prof.items())),
        tuple(
            sorted(
                (
                    int(s.get("tg_user_id") or 0),
                    str(s.get("role") or "specialist"),
                    int(s.get("profession") or 0),
                )
                for s in specs
                if int(s.get("tg_user_id") or 0) > 0
            )
        ),
    )
    prev_fingerprint = bd.get(BD_KEY_PENDING_RECIPIENTS_FINGERPRINT)

    # Если состав pending и получателей не менялся, и не настало время ремайнда —
    # пропускаем цикл уведомлений полностью.
    if current_fingerprint == prev_fingerprint:
        due_for_remind = False
        for pending_ids in current_by_prof.values():
            for chat_id in pending_ids:
                last = float(last_ping.get(chat_id) or 0)
                if (now - last) >= REMIND_EVERY_SEC:
                    due_for_remind = True
                    break
            if due_for_remind:
                break
        if not due_for_remind:
            return

    bd[BD_KEY_PENDING_RECIPIENTS_FINGERPRINT] = current_fingerprint

    # director получает уведомления по всем профессиям
    # обычный специалист — только по своей профессии
    for s in specs:
        tg_uid = int(s.get("tg_user_id"))
        role = str(s.get("role") or "specialist")
        prof = int(s.get("profession") or 0)

        # опционально: если пользователь "заглушен" (например он прямо сейчас в заявках)
        if float(mute_until.get(tg_uid) or 0) > now:
            continue

        # какие pending для него релевантны
        relevant_pending = set()
        if role == "director":
            for p in profs:
                relevant_pending |= current_by_prof.get(p, set())
        else:
            relevant_pending = current_by_prof.get(prof, set())

        if not relevant_pending:
            continue

        # есть ли новые (для мгновенного пуша)
        has_new = False
        if role == "director":
            has_new = any(bool(new_pending_by_prof.get(p)) for p in profs)
        else:
            has_new = bool(new_pending_by_prof.get(prof))

        # нужно ли напоминать по "старым"
        need_remind = False
        for chat_id in relevant_pending:
            last = float(last_ping.get(chat_id) or 0)
            if (now - last) >= REMIND_EVERY_SEC:
                need_remind = True
                break

        if not (has_new or need_remind):
            continue

        # фиксируем last_ping для ВСЕХ текущих pending (чтобы не долбить каждую итерацию)
        for chat_id in relevant_pending:
            last_ping[chat_id] = now

        try:
            if has_new:
                text = "🔔 Появились новые заявки в очереди. Откройте раздел заявок."
            else:
                text = "⏰ Напоминание: в очереди есть заявки, которые ещё не приняты."

            await _send_or_replace_pending_notice(
                bot,
                bd,
                tg_uid=tg_uid,
                text=text,
                reply_markup=_kb_open_chats_for_specialist(role),
            )

        except Exception as e:
            # не роняем джобу
            print(e)
            continue


async def _safe_delete_msg(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def _send_or_replace_pending_notice(bot, bd: dict, *, tg_uid: int, text: str, reply_markup):
    await upsert_notice_tg(
        tg_bot=bot,
        tg_chat_id=int(tg_uid),
        user_id=int(tg_uid),
        kind=NoticeKind.SPECIALIST_PENDING,
        entity_id=int(tg_uid),
        text=text,
        reply_markup=reply_markup,
    )


async def boot_notify_job_ctx(context: ContextTypes.DEFAULT_TYPE):
    """Разовая рассылка после деплоя. Делаем так, чтобы в чате оставалось только 1 такое сообщение."""
    app = context.application
    bd = app.bot_data

    try:
        ids = await get_all_user_ids()
    except Exception as e:
        print(e)
        ids = []

    boot_map = bd.get(BD_KEY_BOOT_NOTICE_MSG)
    if not isinstance(boot_map, dict):
        boot_map = {}
        bd[BD_KEY_BOOT_NOTICE_MSG] = boot_map

    for uid in ids:
        try:
            # 1) удалим прошлое boot-сообщение, если было
            old_mid = boot_map.get(int(uid))
            if old_mid:
                await _safe_delete_msg(app.bot, int(uid), int(old_mid))

            # 2) отправим новое и запомним message_id
            sent = await app.bot.send_message(
                chat_id=int(uid),
                text=BOOT_NOTICE_TEXT,
                reply_markup=refresh_kb(),
            )
            boot_map[int(uid)] = int(sent.message_id)

        except Exception as e:
            print(e)
            continue


async def ofd_reminder_job_ctx(context: ContextTypes.DEFAULT_TYPE):
    """Обёртка для ежедневной джобы напоминаний."""
    await ofd_reminder_job(context.application)


async def menu_button(update, context):
    return await show_main_menu(update, context, title="Чем могу помочь?")


async def _clear_reply_on_any_callback(update, context):
    uid = int(update.effective_user.id)

    cleanup_expired_reply_flags(context, uid)
    clear_chat_reply_flags(context, uid)

    q = update.callback_query
    data = (q.data or "") if q else ""

    # "Ушёл из чатов" = нажал что-то НЕ из cc_/ag_ и НЕ старт чатов,
    # либо явный выход в меню.
    in_chat_system = data.startswith(("cc_", "ag_", "client_chat_start:"))
    leaving_chat_system = (data in ("cc_menu", "back_to_main")) or (not in_chat_system)

    if leaving_chat_system:
        # клиент
        context.user_data.pop("cc_reply_chat_id", None)

        # специалист
        context.user_data.pop("ag_reply_chat_id", None)
        context.user_data.pop("ag_reply_until", None)

        # на всякий случай "оплата/ввод суммы"
        context.user_data.pop("ag_pay_chat_id", None)
        context.user_data.pop("ag_waiting_pay_amount", None)

    return


async def ofd_reminder_job(app):
    """
    Ежедневные напоминания по ОФД/ФН/маркировке
    (логика взята из старого bot.py).
    """
    import datetime

    def _to_date(v):
        if not v:
            return None
        if isinstance(v, datetime.datetime):
            return v.date()
        if isinstance(v, datetime.date):
            return v
        s = str(v).strip()
        # часто приходит DATETIME строкой: 'YYYY-MM-DD HH:MM:SS'
        if len(s) >= 10:
            s = s[:10]
        try:
            return datetime.date.fromisoformat(s)
        except Exception:
            return None

    now = datetime.date.today()

    # ---- 0) Чистим коды, истёкшие более 3 месяцев назад ----
    try:
        await purge_expired_codes_older_than_months(3)
    except Exception as e:
        logger.warning("[purge_expired_codes] failed: %s", e)

    days_notice = [30, 7, 1, 0]
    sent = 0
    skipped_no_user = 0

    for client in await get_all_clients_with_ofd():
        user_id = client.get("user_id")
        if not user_id:
            skipped_no_user += 1
            continue

        inn = client.get("inn")
        ofd_slots = client.get("ofd_slots") or []

        # ---- 1) ОФД по client.ofd_slots ----
        for s in ofd_slots:
            try:
                date = _to_date(s.get("ofd_expires_at"))
                if not date:
                    continue
            except Exception as e:
                print(e)
                continue

            days_left = (date - now).days
            if days_left not in days_notice:
                continue

            try:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ Напоминание:\n"
                        f"Срок действия ОФД по ККТ <b>{s['kkt_slot']}</b> "
                        f"заканчивается через <b>{days_left}</b> дн.\n"
                        f"Дата окончания: <b>{date:%d.%m.%Y}</b>\n"
                        f"ИНН: <code>{inn}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except Exception as e:
                logger.warning("[ofd_reminder] failed to notify %s: %s", user_id, e)

        # ---- 2) ФН + Маркировка по тому же ИНН ----
        try:
            cli = await get_client_by_inn(inn) if inn else None
        except Exception as e:
            print(e)
            cli = None

        if not cli:
            continue

        client_id = cli["id"]

        # ФН
        try:
            fn_slots = await get_client_fn_slots(client_id) or []
        except Exception as e:
            print(e)
            fn_slots = []

        for s in fn_slots:
            raw = s.get("fn_expires_at")
            if not raw:
                continue
            try:
                date = _to_date(raw)
                if not date:
                    continue
            except Exception as e:
                print(e)
                continue

            days_left = (date - now).days
            if days_left not in days_notice:
                continue

            try:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ Напоминание:\n"
                        f"Срок действия ФН ККТ <b>{s['kkt_slot']}</b> "
                        f"заканчивается через <b>{days_left}</b> дн.\n"
                        f"Дата окончания: <b>{date:%d.%m.%Y}</b>\n"
                        f"ИНН: <code>{inn}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except Exception as e:
                logger.warning("[fn_reminder] failed to notify %s: %s", user_id, e)

        # Маркировка
        try:
            mark_slots = await get_client_mark_slots(client_id) or []
        except Exception as e:
            print(e)
            mark_slots = []

        for s in mark_slots:
            raw = s.get("mark_expires_at")
            if not raw:
                continue
            try:
                date = _to_date(raw)
                if not date:
                    continue
            except Exception as e:
                print(e)
                continue

            days_left = (date - now).days
            if days_left not in days_notice:
                continue

            try:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ Напоминание:\n"
                        f"Маркировка срок подписки по ККТ <b>{s['kkt_slot']}</b> "
                        f"заканчивается через <b>{days_left}</b> дн.\n"
                        f"Дата окончания: <b>{date:%d.%m.%Y}</b>\n"
                        f"ИНН: <code>{inn}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except Exception as e:
                logger.warning(
                    "[mark_reminder] failed to notify %s: %s", user_id, e
                )
    # ---- 4) Напоминания по купленным кодам (codes.expires_at) ----
    try:
        exp_codes = await list_codes_expiring_by_expires_at(days_notice) or []
    except Exception as e:
        exp_codes = []
        logger.warning("[codes_expires_reminder] query failed: %s", e)

    for r in exp_codes:
        uid = r.get("user_id")
        if not uid:
            continue

        raw_dt = r.get("expires_at")
        try:
            import datetime as _dt
            if isinstance(raw_dt, _dt.datetime):
                date = raw_dt.date()
            elif isinstance(raw_dt, _dt.date):
                date = raw_dt
            else:
                date = _dt.date.fromisoformat(str(raw_dt)[:10])
        except Exception:
            continue

        try:
            days_left = int(r.get("days_left"))
        except Exception:
            days_left = (date - now).days

        vendor = (r.get("vendor_name") or "ОФД").strip()
        code_val = (r.get("code") or "").strip()

        try:
            await app.bot.send_message(
                chat_id=uid,
                text=(
                        "⚠️ Напоминание:\n"
                        f"Срок действия кода <b>{vendor}</b> заканчивается через <b>{days_left}</b> дн.\n"
                        f"Дата окончания: <b>{date:%d.%m.%Y}</b>\n"
                        + (f"Код: <code>{code_val}</code>" if code_val else "")
                ),
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception as e:
            logger.warning("[codes_expires_reminder] failed to notify %s: %s", uid, e)

    logger.info(
        "[ofd_reminder] sent=%d, skipped_no_user=%d",
        sent,
        skipped_no_user,
    )

def main() -> None:
    # 1) База/схемы
    # try:
    # asyncio.run(ensure_support_chat_schema())
    # except Exception as e:
    # print(e)
    # logger.warning("ensure_support_chat_schema failed", exc_info=True)

    # try:
    # asyncio.run(ensure_agent_chat_schema())
    # except Exception as e:
    # print(e)
    # logger.warning("ensure_agent_chat_schema failed", exc_info=True)

    # 3) Persistence
    state_dir = Path(__file__).parent / "var"
    state_dir.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(filepath=str(state_dir / "bot_state.pkl"))


    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .get_updates_read_timeout(60)
    )
    
    if TELEGRAM_PROXY:
        builder = (
            builder
            .proxy(TELEGRAM_PROXY)
            .get_updates_proxy(TELEGRAM_PROXY)
        )
    
    application = builder.build()
    
    application.job_queue.run_repeating(
        support_pending_notifier_job,
        interval=30,
        first=10,
        name="support_pending_notifier",
    )

    jq = application.job_queue

    # 4) Джобы: разовое уведомление + ежедневные напоминания
    if jq is not None:
        # try:
            # jq.run_once(boot_notify_job_ctx, when=timedelta(seconds=60), name="boot_notice")
        # except Exception as e:
            # print(e)
            # logger.warning("Не удалось запланировать boot_notice", exc_info=True)

        try:
            jq.run_daily(
                ofd_reminder_job_ctx,
                time=dtime(hour=15, minute=15, tzinfo=MOSCOW_TZ),
                name="ofd_daily_reminder",
            )
        except Exception as e:
            print(e)
            logger.warning("Не удалось запланировать ofd_reminder_job", exc_info=True)

    async def start_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        tg_user = update.effective_user
        uid = int(tg_user.id)
        uname = (tg_user.username or "").strip()
        context.user_data["tg_username"] = uname

        # 1) Анти-дабл старт: очень короткий дебаунс (не держим lock на весь роутинг!)
        lock = _get_start_lock(uid)
        async with lock:
            now = time.time()
            last = context.user_data.get("_last_start_ts", 0)
            if now - last < 2:
                # Telegram иногда присылает дубль /start. Специалиста направляем через agent_entry,
                # чтобы не попасть в клиентский flow.
                try:
                    spec = await get_specialist_by_tg(tg_user_id=uid, tg_username=uname)
                except Exception:
                    spec = None
                if spec and not is_partner_specialist(spec):
                    return await agent_entry(update, context)
                return await start(update, context)
            context.user_data["_last_start_ts"] = now

        # 2) Парсим payload (/start ref_1111)
        payload = ""
        start_text = ""
        try:
            start_text = (update.effective_message.text or "").strip()
            parts = start_text.split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ""
        except Exception as e:
            print(e)
            payload = ""
        logger.info(
            "[REF-TG] start_router received: tg_user_id=%s text=%r payload=%r",
            uid, start_text, payload,
        )

        # 3) Referral deep-link
        if payload.startswith("ref_"):
            ref_code = payload[4:].strip()
            if ref_code:
                try:
                    logger.info(
                        "[REF-TG] start_router referral candidate: tg_user_id=%s payload=%r ref_code=%r",
                        uid, payload, ref_code,
                    )
                    res = await apply_referral_code(
                        uid,
                        ref_code,
                        payload,
                        source_platform="telegram",
                    )
                    logger.info(
                        "[REF-TG] start_router referral apply result: tg_user_id=%s payload=%r result=%s",
                        uid, payload, res,
                    )
                    if res.get("ok") and res.get("status") in ("applied", "already_set"):
                        context.user_data["_ref_thanks_pending"] = True

                except Exception as e:
                    print(e)
                    pass

        # 4) Если принудительно как клиент — сразу в регистрацию
        if context.user_data.get("_force_client") is True:
            return await start(update, context)

        # 5) Проверка специалиста (тоже в thread, чтобы не лагало)
        try:
            spec = await get_specialist_by_tg(tg_user_id=uid, tg_username=uname)
            logger.info(
                "[SPEC-TG] start_router get_specialist_by_tg: uid=%s uname=%r → id=%s profession=%s role=%s phone_verified=%s is_active=%s",
                uid, uname,
                (spec or {}).get("id"), (spec or {}).get("profession"),
                (spec or {}).get("role"), (spec or {}).get("phone_verified"),
                (spec or {}).get("is_active"),
            )
            if not spec:
                # Fallback: specialist may be linked only via max_user_id (registered via MAX bot first)
                user_row = await get_user(str(uid))
                max_uid = int((user_row or {}).get("max_user_id") or 0)
                if max_uid:
                    spec = await get_specialist_by_max(max_user_id=max_uid)
                    logger.info("[SPEC-TG] start_router max_uid fallback: max_uid=%s spec_id=%s", max_uid, (spec or {}).get("id"))
        except Exception as e:
            logger.exception("[SPEC-TG] start_router specialist lookup failed: uid=%s uname=%r error=%s", uid, uname, e)
            spec = None

        if spec and not is_partner_specialist(spec):
            context.user_data["_force_client"] = False
            logger.info("[SPEC-TG] start_router routing to agent_entry: uid=%s spec_id=%s", uid, spec.get("id"))
            await send_or_edit(update, "Похоже, вы наш специалист", None, parse_mode=None)
            return await agent_entry(update, context)

        logger.info("[SPEC-TG] start_router routing to client flow: uid=%s spec=%s is_partner=%s", uid, bool(spec), is_partner_specialist(spec) if spec else None)
        # 6) Обычный клиент (или партнёр profession=5) — в регистрацию
        return await start(update, context)

    async def cb_client_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        try:
            await q.answer()
        except Exception:
            pass
        prof = int((q.data or "").split(":")[1])

        uid_int = int(update.effective_user.id)
        uid = str(uid_int)

        if await is_specialist_user(update, context):
            from bot_agent_chat import agent_entry
            return await agent_entry(update, context)

        # ✅ Доступ по подтверждённому телефону (users.phone)
        phone_ok = False
        try:
            if context.user_data.get("phone_confirmed") is True:
                phone_ok = True
            else:
                user_db = await get_user(uid) or {}
                phone_ok = bool(user_db.get("phone"))
                if phone_ok:
                    context.user_data["phone_confirmed"] = True
                    context.user_data["phone"] = user_db.get("phone")
        except Exception as e:
            print(e)
            phone_ok = False

        u = await get_user(str(update.effective_user.id)) or {}
        phone_ok = bool((u.get("phone") or "").strip())

        if not phone_ok:
            await q.edit_message_text(
                "Связь со специалистом доступна после подтверждения телефона в профиле.",
                reply_markup=await nav_kb(context, update.effective_user.id),
            )
            return STATE_IDLE

        return await client_chat_start(update, context, profession=prof)

    # 6) Глобальные callback-обработчики (БЕЗ дублей)
    NAV_GLOBAL = [
        # роутер спец/клиент
        CallbackQueryHandler(cb_client_chat_start, pattern=r"^client_chat_start:\d+$"),

        CallbackQueryHandler(cc_cb_close, pattern=r"^cc_close:"),
        CallbackQueryHandler(cc_cb_close_no, pattern=r"^cc_close_no:"),
        CallbackQueryHandler(cb_dir_sections, pattern=r"^ag_dir_sections$"),
        CallbackQueryHandler(cb_dir_prof, pattern=r"^ag_dir_prof:"),

        # ===== УСЛУГИ (новый раздел, заявки директору) =====
        CallbackQueryHandler(svc_root, pattern=r"^svc_root$"),
        CallbackQueryHandler(svc_gis, pattern=r"^svc_gis$"),
        CallbackQueryHandler(svc_reg, pattern=r"^svc_reg$"),
        CallbackQueryHandler(svc_newbiz, pattern=r"^svc_newbiz$"),
        CallbackQueryHandler(svc_gis_sys, pattern=r"^svc_gis_sys:"),

        # ===== Оформление ИП через Альфа-Банк =====
        CallbackQueryHandler(svc_ip_entry, pattern=r"^svc_ip$"),
        CallbackQueryHandler(ip_wiz_confirm, pattern=r"^ip_wiz_confirm$"),
        CallbackQueryHandler(ip_wiz_change, pattern=r"^ip_wiz_change$"),
        CallbackQueryHandler(ip_wiz_cancel, pattern=r"^ip_wiz_cancel$"),
        CallbackQueryHandler(ip_wiz_city_retry, pattern=r"^ip_wiz_city_retry$"),
        CallbackQueryHandler(ip_submit, pattern=r"^ip_submit$"),
        CallbackQueryHandler(ip_refresh_status, pattern=r"^ip_refresh_status$"),
        CallbackQueryHandler(svc_leaf, pattern=r"^svc_leaf:"),

        # клиентские чаты
        CallbackQueryHandler(cc_cb_menu, pattern=r"^cc_menu$"),
        CallbackQueryHandler(cc_cb_back, pattern=r"^cc_back:"),
        CallbackQueryHandler(cb_type, pattern=r"^cc_type:"),
        CallbackQueryHandler(cb_sub, pattern=r"^cc_sub:"),
        CallbackQueryHandler(cb_category, pattern=r"^cc_cat:"),
        CallbackQueryHandler(cc_cb_open_chat, pattern=r"^cc_open_chat:"),
        CallbackQueryHandler(cb_upd_all, pattern=r"^cc_upd_all:"),
        CallbackQueryHandler(cb_upd_selective, pattern=r"^cc_upd_selective:"),
        CallbackQueryHandler(cb_promo_yes, pattern=r"^cc_promo_yes:"),
        CallbackQueryHandler(cb_promo_no, pattern=r"^cc_promo_no:"),
        CallbackQueryHandler(cc_cb_reply, pattern=r"^cc_reply:"),
        CallbackQueryHandler(cb_my_chats, pattern=r"^(cc_my_chats|my_chats)$"),
        CallbackQueryHandler(cb_mychat_open, pattern=r"^cc_mychat:"),

        # специалист
        CallbackQueryHandler(cb_refresh, pattern=r"^ag_refresh$"),
        CallbackQueryHandler(cb_ref_qr, pattern=r"^ag_ref_qr(?::(telegram|max))?$"),
        CallbackQueryHandler(cb_back_to_chats, pattern=r"^ag_back_to_chats$"),
        CallbackQueryHandler(ag_cb_open_chat, pattern=r"^ag_chat:"),
        CallbackQueryHandler(cb_close, pattern=r"^ag_close:"),
        CallbackQueryHandler(cb_close_yes, pattern=r"^ag_close_yes:"),
        CallbackQueryHandler(cb_close_no, pattern=r"^ag_close_no:"),
        CallbackQueryHandler(cb_close_cancel, pattern=r"^ag_close_cancel:"),
        CallbackQueryHandler(cb_offer_accept, pattern=r"^ag_offer_yes:"),
        CallbackQueryHandler(cb_offer_decline, pattern=r"^ag_offer_no:"),
        CallbackQueryHandler(cb_reassign_chat, pattern="^ag_reassign:\\d+$"),
        CallbackQueryHandler(cb_reassign_to, pattern="^ag_reassign_to:\\d+:\\d+$"),
        
        # каталог/профиль/прочее
        CallbackQueryHandler(show_services_fn, pattern=r"^services_fn$"),
        CallbackQueryHandler(show_services_ofd, pattern=r"^services_ofd$"),
        CallbackQueryHandler(show_services_service, pattern=r"^services_service$"),
        CallbackQueryHandler(show_services_edo, pattern=r"^services_edo$"),
        CallbackQueryHandler(add_to_cart, pattern=r"^product_"),
        CallbackQueryHandler(show_cart, pattern=r"^cart$"),
        CallbackQueryHandler(clear_cart, pattern=r"^clear_cart$"),
        CallbackQueryHandler(remove_from_cart, pattern=r"^del_\d+$"),
        CallbackQueryHandler(cart_inc, pattern=r"^cart_inc:\d+$"),
        CallbackQueryHandler(cart_dec, pattern=r"^cart_dec:\d+$"),
        CallbackQueryHandler(cart_noop, pattern=r"^cart_noop$"),

        CallbackQueryHandler(checkout, pattern=r"^checkout$"),
        CallbackQueryHandler(show_profile, pattern=r"^profile$"),
        CallbackQueryHandler(show_my_orders, pattern=r"^my_orders$"),
        CallbackQueryHandler(my_order_menu, pattern=r"^my_order_menu:\d+$"),
        CallbackQueryHandler(show_referral_qr, pattern=r"^my_referral_qr(?::(telegram|max))?$"),
        CallbackQueryHandler(show_loyalty_history, pattern=r"^loyalty_history$"),
        CallbackQueryHandler(show_referral_progress, pattern=r"^referral_progress$"),
        CallbackQueryHandler(edit_profile_menu, pattern=r"^edit_profile$"),
        CallbackQueryHandler(application_form_start, pattern=r"^application_form$"),
        CallbackQueryHandler(back_to_main, pattern=r"^back_to_main$"),
        CallbackQueryHandler(ident_start, pattern=r"^ident_start$"),
        CallbackQueryHandler(profile_finish_verify, pattern=r"^profile_finish_verify$"),
        CallbackQueryHandler(check_payment, pattern=r"^(check_payment|pay_check:.+)$"),
        CallbackQueryHandler(extra_verify_start, pattern=r"^extra_verify$"),
        CallbackQueryHandler(svc_root, pattern=r"^services$"),
        CallbackQueryHandler(show_shop, pattern=r"^buy$"),  # магазин/каталог
        CallbackQueryHandler(cb_shop_consult, pattern=r"^shop_consult:[a-z0-9_]+$"),
        CallbackQueryHandler(usl_gis_menu, pattern=r"^usl_gis$"),
        CallbackQueryHandler(usl_reg_menu, pattern=r"^usl_reg$"),
        CallbackQueryHandler(usl_newbiz_menu, pattern=r"^usl_newbiz$"),
        CallbackQueryHandler(usl_gis_sys_menu, pattern=r"^usl_gis_sys:[a-z_]+$"),
        CallbackQueryHandler(usl_leaf_stub, pattern=r"^usl_leaf:"),
        CallbackQueryHandler(show_shop_equipment, pattern=r"^shop_equipment$"),
        CallbackQueryHandler(show_shop_paper, pattern=r"^shop_paper$"),
        CallbackQueryHandler(eq_root_cb, pattern=r"^eq_root:\d+$"),
        CallbackQueryHandler(eq_category_cb, pattern=r"^eq_cat:\d+:\d+$"),
        CallbackQueryHandler(eq_view_cb, pattern=r"^eq_view:\d+:\d+:\d+$"),
        CallbackQueryHandler(eq_add_cb, pattern=r"^eq_add:\d+:\d+:\d+$"),
        CallbackQueryHandler(ext_ofd_provider_cb, pattern=r"^ext_ofd_p:\d+$"),
        CallbackQueryHandler(ext_ofd_add_cb, pattern=r"^ext_ofd_add:\d+:\d+$"),
        CallbackQueryHandler(ext_srv_provider_cb, pattern=r"^ext_srv_p:\d+$"),
        CallbackQueryHandler(ext_srv_add_cb, pattern=r"^ext_srv_add:\d+:\d+$"),
        CallbackQueryHandler(ext_edo_provider_cb, pattern=r"^ext_edo_p:\d+$"),
        CallbackQueryHandler(ext_edo_add_cb, pattern=r"^ext_edo_add:\d+:\d+$"),
        CallbackQueryHandler(ext_srv_ord_provider_cb, pattern=r"^ext_srv_ord_p:\d+$"),
        CallbackQueryHandler(ext_srv_ord_add_cb, pattern=r"^ext_srv_ord_add:\d+:\d+$"),
        
        CallbackQueryHandler(renew_inn, pattern=r"^input_new_inn$"),
        CallbackQueryHandler(renew_kkt_factory_number, pattern=r"^input_new_kkt_number$"),
        
        CallbackQueryHandler(show_users_with_orders, pattern=r"^users_with_orders$"),
        CallbackQueryHandler(show_orders_of_user, pattern=r"^user_with_orders:\d+$"),
        CallbackQueryHandler(order_menu, pattern=r"^show_order:\d+$"),
        CallbackQueryHandler(dir_set_service_code_start, pattern=r"^dir_srv_code:\d+:\d+$"),
        CallbackQueryHandler(close_order_confirm, pattern=r"^close_order_start:\d+$"),
        CallbackQueryHandler(close_order_confirm_yes, pattern=r"^close_order_finish:\d+$"),

    ]

    ADMIN_GLOBAL = [
        CallbackQueryHandler(admin_menu, pattern=r"^admin_menu$"),
        CallbackQueryHandler(admin_show_stats, pattern=r"^admin_stats$"),
        CallbackQueryHandler(admin_show_recent, pattern=r"^admin_recent$"),
        CallbackQueryHandler(admin_add_codes_start, pattern=r"^admin_add_codes$"),
        CallbackQueryHandler(admin_give_code, pattern=r"^admin_give_code$"),
        CallbackQueryHandler(admin_ofd_broadcast_all, pattern=r"^admin_ofd_broadcast_all$"),
    ]

    MYCODES_GLOBAL = [
        CallbackQueryHandler(show_my_codes, pattern=r"^(my_codes|codes_prev:\d+|codes_next:\d+)$"),
        CallbackQueryHandler(show_profile_ofd, pattern=r"^profile_ofd$"),
    ]

    CONSENT_GLOBAL = [
        CallbackQueryHandler(show_consent, pattern=r"^consent$"),
    ]

    EDIT_GLOBAL = [
        CallbackQueryHandler(edit_profile_menu, pattern=r"^edit_profile$"),
        CallbackQueryHandler(edit_inn_prompt, pattern=r"^edit_inn$"),
    ]

    # 7) Таблица состояний ConversationHandler (БЕЗ дублей ключей)
    states = {
        # ===== Анкета (заявка/оформление) =====
        STATE_FORM_TP: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, form_tp),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_ADDR: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, form_addr),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_ACT: [
            CallbackQueryHandler(form_act_cb, pattern=r"^act_(food|nonfood|services|wholesale|marketplaces)$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_ASSORT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, form_assort),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_SERVICES: [
            CallbackQueryHandler(form_services_cb, pattern=r"^svc_(chz|merk|ega|stat|done)$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_SNO: [
            CallbackQueryHandler(form_sno_cb, pattern=r"^sno_(usn6|usn15|patent|osn|self)$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_MARKED: [
            CallbackQueryHandler(form_marked_cb, pattern=r"^marked_(yes|no)$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_FNTERM: [
            CallbackQueryHandler(form_fnterm_cb, pattern=r"^fn_(15|36)_term$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_OFD: [
            CallbackQueryHandler(form_ofd_cb, pattern=r"^ofd_(any|first_opt|platform_opt)$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_FORM_BANKS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, form_banks),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],

        # ===== Главный экран, каталог, корзина =====
        STATE_IDLE: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL, *CONSENT_GLOBAL, *EDIT_GLOBAL,
            CallbackQueryHandler(cb_check_subscription_tg, pattern=r"^check_subscription$"),
            CallbackQueryHandler(start_registration, pattern=r"^register$"),
            # Сбор данных для заявки на ИП — ловим текстовый ввод только если
            # установлен флаг _ip_pending_edit (внутри ip_save_field).
            MessageHandler(filters.TEXT & ~filters.COMMAND, ip_save_field),
        ],
        STATE_CONSENT: [
            *CONSENT_GLOBAL,
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_PRODUCT_SELECT: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_CART: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],

        # ===== Checkout — сбор недостающих данных =====
        STATE_CO_EMAIL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_email),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_CO_INN: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_inn),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_CO_KKT_FACTORY_NUMBER: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_kkt_factory_number),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_CO_CITY: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_city),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_CO_BONUS: [
            CallbackQueryHandler(handle_co_bonus_use, pattern=r"^co_bonus_use$"),
            CallbackQueryHandler(handle_co_bonus_skip, pattern=r"^co_bonus_skip$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_CO_ADDRESS: [
            CallbackQueryHandler(handle_co_addr_use, pattern=r"^co_addr_use$"),
            CallbackQueryHandler(handle_co_addr_new, pattern=r"^co_addr_new$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_address),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],

        # ===== Админ =====
        STATE_ADMIN_ADD_CODES: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_codes_receive),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
STATE_DIR_SET_SERVICE_CODE: [
    MessageHandler(filters.TEXT & ~filters.COMMAND, dir_set_service_code_receive),
    *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
],

        # ===== Клиентские чаты =====
        CC_WAIT_TYPE: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        CC_WAIT_CATEGORY: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        CC_WAIT_PROMO_CHOICE: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        CC_WAIT_PROMO_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_promo_input),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        CC_WAIT_CONTENT: [
            MessageHandler(filters.ALL & ~filters.COMMAND, on_content_message),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        CC_WAIT_REPLY: [
            MessageHandler(filters.ALL & ~filters.COMMAND, on_reply_text),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],

        # ===== Специалист =====
        AG_WAIT_PHONE: [
            MessageHandler(filters.CONTACT, on_agent_contact),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        AG_MENU_CHATS: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        AG_CHAT_VIEW: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        AG_WAIT_REPLY: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        AG_WAIT_PAY_AMOUNT: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        AG_CLOSE_CONFIRM: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],

        # ===== Редактирование профиля =====
        STATE_EDIT_MENU: [
            CallbackQueryHandler(edit_firstname_prompt, pattern=r"^edit_firstname$"),
            CallbackQueryHandler(edit_lastname_prompt, pattern=r"^edit_lastname$"),
            CallbackQueryHandler(edit_phone_prompt, pattern=r"^edit_phone$"),
            CallbackQueryHandler(edit_email_prompt, pattern=r"^edit_email$"),
            CallbackQueryHandler(edit_inn_prompt, pattern=r"^edit_inn$"),
            CallbackQueryHandler(edit_city_prompt, pattern=r"^edit_city$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL, *EDIT_GLOBAL,
        ],
        STATE_EDIT_FIRSTNAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_firstname),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_EDIT_LASTNAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_lastname),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_EDIT_PHONE: [
            MessageHandler(filters.CONTACT, save_phone_from_contact),
            MessageHandler(filters.TEXT & ~filters.COMMAND, reject_manual_phone_in_edit),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_EDIT_EMAIL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_email),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_EDIT_INN: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_inn),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        STATE_EDIT_CITY: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_city),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],

        # ===== Новая FSM идентификации =====
        S0_START: [
            CallbackQueryHandler(cb_check_subscription_tg, pattern=r"^check_subscription$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_name),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        S1_IDENT: [
            CallbackQueryHandler(cb_check_subscription_tg, pattern=r"^check_subscription$"),
            CallbackQueryHandler(cb_consent, pattern=r"^(accept_consent|go_guest)$"),
            CallbackQueryHandler(cb_ident, pattern=r"^(ident_share_contact|ident_enter_inn|ident_cancel)$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        S2_WAIT_CONTACT: [
            MessageHandler(filters.CONTACT, on_contact),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        S4_WAIT_INN: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_inn),
            CallbackQueryHandler(skip_inn_cb, pattern=r"^skip_inn$"),
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        S0_GUEST: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
        S5_HOME: [
            *NAV_GLOBAL, *ADMIN_GLOBAL, *MYCODES_GLOBAL,
        ],
    }
    async def _safe_log_event(**kwargs):
        try:
            await log_event(**kwargs)
        except Exception as e:
            try:
                logger.debug("log_event failed: %s", e)
            except Exception:
                pass

    async def _analytics_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q:
            return
        data = q.data or ""
        name = data.split(":", 1)[0] if data else None

        asyncio.create_task(_safe_log_event(
            event_type="callback",
            tg_user_id=int(update.effective_user.id) if update.effective_user else None,
            chat_id=int(update.effective_chat.id) if update.effective_chat else None,
            message_id=int(q.message.message_id) if q.message else None,
            update_id=getattr(update, "update_id", None),
            name=name,
            callback_data=data,
            payload={
                "in_chat_system": data.startswith(("cc_", "ag_", "client_chat_start:")),
            },
        ))

    async def _analytics_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        m = update.effective_message
        if not m:
            return
        text = (m.text or m.caption or "")
        has_file = bool(getattr(m, "document", None) or getattr(m, "photo", None) or getattr(m, "video", None) or
                        getattr(m, "audio", None) or getattr(m, "voice", None) or getattr(m, "video_note", None))

        # имя события: command / text / other
        name = "command" if (text or "").startswith("/") else "text"

        asyncio.create_task(_safe_log_event(
            event_type="message",
            tg_user_id=int(update.effective_user.id) if update.effective_user else None,
            chat_id=int(update.effective_chat.id) if update.effective_chat else None,
            message_id=int(m.message_id),
            update_id=getattr(update, "update_id", None),
            name=name,
            text_len=len(text) if text else 0,
            has_file=has_file,
            payload={
                "has_file": has_file,
            },
        ))

    # 8) ConversationHandler + команды + fallback
    application.add_handler(
        CallbackQueryHandler(_clear_reply_on_any_callback, pattern=r"^", block=False),
        group=-2
    )
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_router),
            CallbackQueryHandler(start_registration, pattern=r"^register$"),

        ],
        states=states,
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        allow_reentry=True,
        name="main_conv",
        persistent=True,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, referral_trigger),
        group=-1
    )
    application.add_handler(CallbackQueryHandler(_analytics_on_callback, pattern=r"^", block=False), group=-3)
    application.add_handler(MessageHandler(filters.ALL, _analytics_on_message, block=False), group=-3)
    application.add_handler(conv_handler)
    application.add_handler(
        CallbackQueryHandler(cb_any_router, pattern=r"^")
    )
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("reg", start_registration))
    application.add_handler(CommandHandler("reset_me", reset_me))
    application.add_handler(CommandHandler("wipe_me", wipe_me))
    application.add_handler(CommandHandler("delete_me", delete_me))
    application.add_handler(CommandHandler("refresh", refresh_ui))
    # запасной обработчик на refresh
    application.add_handler(CallbackQueryHandler(refresh_ui, pattern=r"^refresh$"))

    # текстовый fallback — в самом конце.
    # Если установлен флаг ожидания ввода для ИП, делегируем ip_save_field
    # (нужно для пользователей без активного состояния ConversationHandler).
    async def _text_fallback_with_ip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if context.user_data.get(_IP_PENDING_EDIT_KEY):
            return await ip_save_field(update, context)
        return await text_fallback(update, context)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text_fallback_with_ip))

    # 9) Глобальный error-handler
    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled error", exc_info=context.error)

    application.add_error_handler(_on_error)

    # 10) Старт бота
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
