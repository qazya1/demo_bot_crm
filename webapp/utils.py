# utils.py
import asyncio
import logging
from pathlib import Path
from datetime import datetime
import httpx
from fastapi import HTTPException

try:
    from .config import BOT_TOKEN, MAX_BOT_TOKEN, MAX_BOT_NAME, WEBAPP_URL, FILES_DIR
except ImportError:  # pragma: no cover - fallback for standalone запусков
    from config import BOT_TOKEN, MAX_BOT_TOKEN, MAX_BOT_NAME, WEBAPP_URL, FILES_DIR
import os
import sys

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
import db
import tinkoff
from services.checkout_bonus import finalize_bonus_discount, cancel_bonus_discount
from bot.cross_notify import notify_specialist_all_platforms

logger = logging.getLogger(__name__)

MAX_API_BASE_URL = "https://platform-api.max.ru"

# Глобальные переменные
PAYMENT_WATCH_TASKS: dict[str, asyncio.Task] = {}
NOTICE_MSG_IDS: dict[tuple[int, int], int] = {}
MAX_NOTICE_MSG_IDS: dict[tuple[int, int], str] = {}
PENDING_CLIENT_MSG_LIMIT = 5

# ── asyncio locks для защиты read-await-write паттернов ──────────────
_tg_notice_locks: dict[tuple[int, int], asyncio.Lock] = {}
_max_notice_locks: dict[tuple[int, int], asyncio.Lock] = {}
_pending_msg_locks: dict[int, asyncio.Lock] = {}
_payment_finalize_locks: dict[str, asyncio.Lock] = {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


PAYMENT_WAIT_TIMEOUT_SEC = _env_int("PAYMENT_WAIT_TIMEOUT_SEC", 30 * 60)


def _get_tg_notice_lock(key: tuple[int, int]) -> asyncio.Lock:
    if key not in _tg_notice_locks:
        _tg_notice_locks[key] = asyncio.Lock()
    return _tg_notice_locks[key]


def _get_max_notice_lock(key: tuple[int, int]) -> asyncio.Lock:
    if key not in _max_notice_locks:
        _max_notice_locks[key] = asyncio.Lock()
    return _max_notice_locks[key]


def _get_pending_msg_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _pending_msg_locks:
        _pending_msg_locks[chat_id] = asyncio.Lock()
    return _pending_msg_locks[chat_id]


def _get_payment_finalize_lock(payment_id: str) -> asyncio.Lock:
    key = str(payment_id)
    if key not in _payment_finalize_locks:
        _payment_finalize_locks[key] = asyncio.Lock()
    return _payment_finalize_locks[key]


def _chat_dir(chat_id: int) -> Path:
    d = FILES_DIR / f"chat_{int(chat_id)}"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _is_paid_status(status: str) -> bool:
    return str(status).upper() in ("CONFIRMED", "AUTHORIZED")

async def _run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

async def can_write(*, chat_id: int, role_from_token: str, chat_status: str) -> bool:
    st = (chat_status or "").strip().lower()
    role = (role_from_token or "").strip().lower()

    # 1) Закрытая заявка — никто не пишет
    if st == "closed":
        return False

    # 2) Pending:
    # - директору можно писать
    # - клиенту можно до PENDING_CLIENT_MSG_LIMIT сообщений
    # - остальным нельзя до принятия
    if st == "pending":
        if role == "director":
            return True

        if role == "client":
            try:
                cnt = await db.count_chat_messages_by_sender(int(chat_id), 0)
            except Exception as e:
                logger.error(f"Error counting client messages for chat {chat_id}: {e}")
                cnt = PENDING_CLIENT_MSG_LIMIT
            return cnt < PENDING_CLIENT_MSG_LIMIT

        return False
    # 3) Active — можно
    return True


async def get_pending_client_messages_left(chat_id: int) -> int:
    """Сколько сообщений клиент ещё может отправить в pending-чате до принятия."""
    try:
        cnt = await db.count_chat_messages_by_sender(int(chat_id), 0)
    except Exception as e:
        logger.error(f"Error counting pending messages for chat {chat_id}: {e}")
        return 0
    return max(0, PENDING_CLIENT_MSG_LIMIT - int(cnt))


async def resolve_effective_role(role_from_token: str, actor_id_raw: str, chat_id: int) -> str:
    """
    Если токен выдан с ролью 'client', но actor является специалистом, НАЗНАЧЕННЫМ
    на этот чат (dual-role пользователь), возвращает 'agent'. Иначе возвращает исходную роль.

    Проверяем только по назначенному специалисту конкретного чата, чтобы не блокировать
    запись в pending-чатах (где specialist_id ещё не выставлен).
    """
    if str(role_from_token or "").strip().lower() != "client":
        return role_from_token

    actor_raw = str(actor_id_raw or "").strip()
    if not actor_raw.isdigit():
        return role_from_token

    try:
        actor_int = int(actor_raw)
        chat = await db.get_chat_by_id(int(chat_id))
        if not chat:
            return role_from_token

        specialist_id = int(chat.get("specialist_id") or 0)
        if not specialist_id:
            return role_from_token  # чат ещё не принят — не апгрейдируем

        spec = await db.get_specialist_by_id(specialist_id)
        if not spec or db.is_partner_specialist(spec):
            return role_from_token

        spec_tg = int(spec.get("tg_user_id") or 0)
        spec_max = int(spec.get("max_user_id") or 0)

        if actor_int and actor_int in (spec_tg, spec_max):
            logger.info(
                "resolve_effective_role: actor %s is assigned specialist for chat %s, upgrading client→agent",
                actor_raw, chat_id,
            )
            return "agent"
    except Exception as e:
        logger.warning("resolve_effective_role failed for actor %s chat %s: %s", actor_raw, chat_id, e)

    return role_from_token

async def get_personal_data(chat: dict, role_from_token: str):
    role = (role_from_token or "").strip().lower()
    # удаляем поля по умолчанию — клиент никогда не получает чужие данные
    chat.pop("client_phone", None)
    chat.pop("client_inn", None)

    if role in ("agent", "director"):
        client_id = chat.get("client_user_id")
        inn = await db.get_user_inn(internal_user_id=client_id)
        phone = await db.get_user_phone(internal_user_id=client_id)
        we_added = await db.get_user_we_added(internal_user_id=client_id)

        chat["client_we_added"] = bool(we_added)
        # агент видит только факт наличия
        chat["client_has_phone"] = bool(phone)
        chat["client_has_inn"] = bool(inn)

        if role == "director":
            # директор видит реальные данные
            chat["client_phone"] = phone or ""
            chat["client_inn"] = inn or ""

    return chat

async def finalize_chat_payment_success(*, payment_id: str, chat_id: int | None = None, amount: int | None = None, source: str = "unknown") -> bool:
    """
    Идемпотентно фиксирует успешную оплату в БД и запускает downstream-уведомления.
    Возвращает True, если именно этот вызов выполнил финализацию; False, если платёж уже был CONFIRMED.
    """
    payment_key = str(payment_id)
    lock = _get_payment_finalize_lock(payment_key)

    async with lock:
        payment_row = await db.get_payment_by_payment_id(payment_key)
        if not payment_row:
            logger.warning("Payment %s finalization from %s skipped: payment row not found", payment_key, source)
            return False

        current_status = str(payment_row.get("status") or "").upper()
        if current_status == "CONFIRMED":
            logger.info("Payment %s already CONFIRMED, running repair checks from %s", payment_key, source)
            final_chat_id = int(payment_row.get("chat_id") or chat_id or 0)
            paid_cash_amount = int(payment_row.get("cash_amount_rub") or amount or payment_row.get("amount_rub") or 0)
            full_amount_rub = int(payment_row.get("amount_rub") or paid_cash_amount)
            bonus_points_applied = int(payment_row.get("bonus_points_applied") or 0)
            bonus_discount_rub = int(payment_row.get("bonus_discount_rub") or 0)
            if final_chat_id > 0:
                try:
                    await _ensure_payment_success_chat_message(
                        chat_id=final_chat_id,
                        payment_id=payment_key,
                        paid_cash_amount=paid_cash_amount,
                        bonus_points_applied=bonus_points_applied,
                        bonus_discount_rub=bonus_discount_rub,
                    )
                except Exception:
                    logger.warning(
                        "Repair of chat visibility failed for payment %s",
                        payment_key,
                        exc_info=True,
                    )
                try:
                    await _create_order_from_chat_payment(payment_key, final_chat_id, full_amount_rub)
                except Exception:
                    logger.warning("Order repair failed for already confirmed payment %s", payment_key, exc_info=True)
            return False

        final_chat_id = int(payment_row.get("chat_id") or chat_id or 0)
        paid_cash_amount = int(payment_row.get("cash_amount_rub") or amount or payment_row.get("amount_rub") or 0)
        full_amount_rub = int(payment_row.get("amount_rub") or paid_cash_amount)
        bonus_points_applied = int(payment_row.get("bonus_points_applied") or 0)
        bonus_discount_rub = int(payment_row.get("bonus_discount_rub") or 0)

        await db.set_payment_status_by_payment_id(payment_key, "CONFIRMED")
        logger.info("Payment %s confirmed in DB from %s", payment_key, source)

        if final_chat_id > 0:
            await _ensure_payment_success_chat_message(
                chat_id=final_chat_id,
                payment_id=payment_key,
                paid_cash_amount=paid_cash_amount,
                bonus_points_applied=bonus_points_applied,
                bonus_discount_rub=bonus_discount_rub,
            )
            logger.info("Added payment confirmation message to chat %s", final_chat_id)

            try:
                await _create_order_from_chat_payment(payment_key, final_chat_id, full_amount_rub)
            except Exception:
                logger.warning(
                    "Order creation from chat payment %s failed — не критично",
                    payment_key,
                    exc_info=True,
                )

            try:
                await _award_loyalty_for_chat_payment(payment_key, final_chat_id, paid_cash_amount)
            except Exception:
                logger.warning(
                    "Loyalty award failed for chat payment %s — не критично",
                    payment_key,
                    exc_info=True,
                )
        else:
            logger.warning("Payment %s confirmed but chat_id is missing (source=%s)", payment_key, source)

        return True


def _build_payment_success_message(*, payment_id: str, paid_cash_amount: int, bonus_points_applied: int, bonus_discount_rub: int) -> str:
    return (
        f"✅ Оплата подтверждена: {paid_cash_amount} ₽"
        + (f"\nСписано баллами: {bonus_points_applied} (≈ {bonus_discount_rub} ₽)" if bonus_points_applied > 0 else "")
        + f"\nID платежа: {payment_id}"
    )


async def _ensure_payment_success_chat_message(
    *,
    chat_id: int,
    payment_id: str,
    paid_cash_amount: int,
    bonus_points_applied: int,
    bonus_discount_rub: int,
) -> None:
    if await db.has_chat_payment_success_message(int(chat_id), str(payment_id)):
        return

    await db.add_chat_message(
        int(chat_id),
        1,
        _build_payment_success_message(
            payment_id=payment_id,
            paid_cash_amount=paid_cash_amount,
            bonus_points_applied=bonus_points_applied,
            bonus_discount_rub=bonus_discount_rub,
        ),
    )


async def watch_payment_until_paid(*, payment_id: str, chat_id: int, amount: int, timeout_sec: int | None = None):
    started = asyncio.get_running_loop().time()
    timeout = PAYMENT_WAIT_TIMEOUT_SEC if timeout_sec is None else int(timeout_sec)

    logger.info(f"Starting payment watch for payment_id: {payment_id}, chat_id: {chat_id}, amount: {amount}")

    # если уже подтвердили — выходим
    p = await db.get_payment_by_payment_id(payment_id)
    if p and str(p.get("status") or "").upper() == "CONFIRMED":
        logger.info(f"Payment {payment_id} already confirmed")
        return

    reservation_key = str((p or {}).get("bonus_reservation_key") or "")
    while True:
        if asyncio.get_running_loop().time() - started > timeout:
            logger.warning(f"Payment watch timeout for payment_id: {payment_id}")
            if reservation_key:
                try:
                    await cancel_bonus_discount(reservation_key)
                except Exception:
                    logger.warning("Bonus reservation cancel failed for payment %s", payment_id, exc_info=True)
            await db.set_payment_status_by_payment_id(payment_id, "FAILED")
            return

        try:
            resp = await _run_blocking(tinkoff.get_payment_state, payment_id=payment_id)
            st = str((resp or {}).get("Status") or (resp or {}).get("status") or "")
            logger.debug(f"Payment {payment_id} status: {st}")
        except Exception as e:
            logger.error(f"Error getting payment state for {payment_id}: {e}")
            st = ""

        if _is_paid_status(st):
            if reservation_key:
                try:
                    await finalize_bonus_discount(reservation_key)
                except Exception:
                    logger.warning("Bonus finalize failed for payment %s", payment_id, exc_info=True)
            await finalize_chat_payment_success(payment_id=payment_id, chat_id=chat_id, amount=amount, source="polling")
            return

        await asyncio.sleep(20)


async def _award_loyalty_for_chat_payment(payment_id: str, chat_id: int, amount: int) -> None:
    """Начислить баллы за оплату услуги в чате (best-effort)."""
    from services.loyalty_engine import is_loyalty_enabled, award_points_for_purchase

    if not await is_loyalty_enabled():
        return

    # Получить данные платежа (client_user_id из чата)
    payment = await db.get_payment_by_payment_id(payment_id)
    if not payment:
        return

    scp_id = int(payment.get("id") or 0)
    p_chat_id = int(payment.get("chat_id") or chat_id)

    # Получить client_user_id из чата
    chat = await db.get_chat(p_chat_id)
    if not chat:
        return

    internal_user_id = int(chat.get("client_user_id") or 0)
    if not internal_user_id:
        return

    await award_points_for_purchase(
        internal_user_id=internal_user_id,
        order_id=scp_id,
        order_amount_rub=float(amount),
        product_type="consultation",
    )


async def _notify_payment_success_to_staff(chat_id: int, payment_id: str, amount: int) -> None:
    """Уведомить специалиста и директоров об успешной оплате в чате."""
    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        return

    targets: list[dict] = []
    target_ids: set[int] = set()

    specialist_id = int(chat.get("specialist_id") or 0)
    if specialist_id:
        specialist = await db.get_specialist_by_id(specialist_id)
        if specialist:
            sid = int(specialist.get("id") or 0)
            if sid:
                target_ids.add(sid)
            targets.append(specialist)

    directors = await db.list_directors()
    for director in directors or []:
        did = int(director.get("id") or 0)
        if did and did in target_ids:
            continue
        if did:
            target_ids.add(did)
        targets.append(director)

    if not targets:
        return

    text = (
        "✅ Клиент оплатил чат\n"
        f"Чат #{int(chat_id)}\n"
        f"Сумма: {int(amount)} ₽\n"
        f"payment_id: {payment_id}"
    )
    btns = [[{"type": "callback", "text": "💬 Ответить", "payload": f"ag_chat:{int(chat_id)}"}]]

    for spec in targets:
        try:
            await notify_specialist_all_platforms(
                spec,
                text,
                tg_reply_markup={"inline_keyboard": [[{"text": "💬 Ответить", "callback_data": f"ag_chat:{int(chat_id)}"}]]},
                max_keyboard_buttons=btns,
            )
        except Exception:
            logger.warning(
                "Payment notify failed to specialist_id=%s for chat_id=%s",
                spec.get("id"),
                chat_id,
                exc_info=True,
            )


async def _create_order_from_chat_payment(payment_id: str, chat_id: int, amount: int) -> None:
    """Создать запись в orders (сделки) из оплаты в чате поддержки."""
    # Получить данные платежа
    payment = await db.get_payment_by_payment_id(payment_id)
    if not payment:
        return

    specialist_id = int(payment.get("specialist_id") or 0)

    # Получить данные чата (client_user_id, profession, category)
    chat = await db.get_chat_by_id(int(payment.get("chat_id") or chat_id))
    if not chat:
        return

    if specialist_id <= 0:
        specialist_id = int(chat.get("specialist_id") or 0)

    client_user_id = int(chat.get("client_user_id") or 0)
    if not client_user_id:
        return

    # Определяем источник по данным чата
    source = "chat"
    source_platform = str(chat.get("source") or "").strip().lower()

    # Описание из категории/типа чата
    profession = int(chat.get("profession") or 0)
    prof_names = {
        0: "Бухгалтер",
        1: "Тех. специалист",
        2: "Директор",
        3: "Консультант",
        4: "Разрешительная документация",
    }
    prof_name = prof_names.get(profession, "Специалист")
    category = chat.get("category") or ""
    request_type = chat.get("request_type") or ""
    description = f"Оплата в чате — {prof_name}"
    if category:
        description += f" ({category})"
    if source_platform:
        description += f" [{source_platform}]"

    # Проверяем, нет ли уже order с таким payment_id (защита от дублей)
    existing = await db.get_order_by_payment_id(payment_id)
    if existing:
        logger.info(f"Order already exists for payment_id {payment_id}, running pipeline repair")
        existing_order_id = int(existing.get("id") or existing.get("order_id") or 0)
        if existing_order_id:
            await db.ensure_order_post_payment_pipeline(existing_order_id, source_hint="chat_existing_repair")
        return

    order_id = await db.create_order_from_chat_payment(
        user_id=client_user_id,
        specialist_id=(specialist_id if specialist_id > 0 else None),
        amount=amount,
        payment_id=payment_id,
        source=source,
        description=description,
    )
    logger.info(f"Created order #{order_id} from chat payment {payment_id}")


def _chat_brief(chat: dict, role: str, message_text: str | None = None) -> str:
    chat_id = int(chat.get("id") or 0)
    req = (chat.get("request_type") or "").strip().lower()
    cat = (chat.get("category") or "")

    if req == "order" and cat:
        title = f"Заказ #{cat}"
    else:
        title = cat or chat.get("request_type") or "Обращение"

    if role != "client":
        base = f"Чат #{chat_id}\nТема: {title}"
    else:
        base = f"Чат #{chat_id}\nТема: {title}\nКлиент: {chat.get('client_name')}"

    if message_text:
        snippet = message_text[:300]
        base += f"\n\n{snippet}"

    return base

async def tg_send_and_cleanup_previous(
    key: tuple[int, int],
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
):
    """
    Всегда отправляем новое уведомление.
    Если было предыдущее уведомление для этого key=(tg_user_id, support_chat_id) — удаляем его.
    Lock по ключу предотвращает race condition при конкурентных уведомлениях.
    """
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set, skipping Telegram send")
        return

    async with _get_tg_notice_lock(key):
        prev_mid = int(NOTICE_MSG_IDS.get(key) or 0)

        url_send = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": int(chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        new_mid = 0
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url_send, json=payload)
                if r.status_code != 200:
                    logger.error(f"Telegram API error (sendMessage): {r.status_code}, {r.text}")
                    return
                data = r.json()
                new_mid = int((((data or {}).get("result") or {}).get("message_id")) or 0)
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
            return

        if not new_mid:
            return

        NOTICE_MSG_IDS[key] = new_mid

        if prev_mid and prev_mid != new_mid:
            url_del = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
            del_payload = {"chat_id": int(chat_id), "message_id": int(prev_mid)}
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    dr = await client.post(url_del, json=del_payload)
                    if dr.status_code != 200:
                        logger.info(f"Telegram deleteMessage failed: {dr.status_code}, {dr.text}")
            except Exception as e:
                logger.info(f"Error deleting Telegram message: {e}")

def webapp_markup(chat_id: int, url: str, role: str, order_id: int | None = None) -> dict:
    if role == "client":
        if order_id:
            return {
                "inline_keyboard": [[
                    {"text": "💬 Ответить", "web_app": {"url": url}}
                ],
                [{"text": "К заказу", "callback_data": f"my_order_menu:{order_id}"}],
                [{"text": "🔙 В меню", "callback_data": "back_to_main"}]]
            }
        return {
            "inline_keyboard": [[
                {"text": "💬 Ответить", "web_app": {"url": url}}
            ],
            [{"text": "🔙 В меню", "callback_data": "back_to_main"}]]
        }
    else:
        if order_id:
            return {
                "inline_keyboard": [[
                    {"text": "💬 Ответить", "web_app": {"url": url}}
                ],
                [{"text": "К заказу", "callback_data": f"show_order:{order_id}"}]]
            }
        return {
            "inline_keyboard": [[
                {"text": "💬 Ответить", "web_app": {"url": url}}
            ]]
        }


def _max_open_chat_button(url: str) -> dict:
    """Build a MAX button for opening the chat without exposing tokens in logs.

    MAX deep links with startapp must be regular links: this matches the
    working bot menu buttons. Direct webapp URLs continue to use open_app.
    """
    url = str(url or "")
    normalized_url = url.lower()
    if "max.ru" in normalized_url and "startapp=" in normalized_url:
        return {"type": "link", "text": "💬 Ответить", "url": url}
    return {"type": "open_app", "text": "💬 Ответить", "web_app": url}


def _find_max_open_chat_button(keyboard_buttons: list[list[dict]] | None) -> dict | None:
    for row in keyboard_buttons or []:
        for button in row or []:
            if isinstance(button, dict) and button.get("text") in ("💬 Ответить", "💬 Открыть чат"):
                return button
    return None


def _log_max_open_chat_button(keyboard_buttons: list[list[dict]] | None) -> None:
    button = _find_max_open_chat_button(keyboard_buttons)
    if not button:
        logger.info("MAX notification open chat button: missing")
        return

    target = str(button.get("url") or button.get("web_app") or "")
    normalized_target = target.lower()
    payload = str(button.get("payload") or "")
    logger.info(
        "MAX notification open chat button: type=%s has_startapp=%s has_payload=%s",
        button.get("type"),
        "startapp=" in normalized_target,
        bool(payload) or "payload=" in normalized_target,
    )


def max_webapp_markup(chat_id: int, url: str, role: str) -> list[list[dict]]:
    if role == "client":
        rows = [
            [_max_open_chat_button(url)],
            [{"type": "callback", "text": "🔙 В меню", "payload": "back_to_main"}],
        ]
    else:
        rows = [
            [_max_open_chat_button(url)],
        ]
    return rows


async def max_send_and_cleanup_previous(
    key: tuple[int, int],
    user_id: int,
    text: str,
    keyboard_buttons: list[list[dict]] | None = None,
):
    if not MAX_BOT_TOKEN:
        logger.warning("MAX_BOT_TOKEN not set, skipping MAX send")
        return

    async with _get_max_notice_lock(key):
        prev_mid = str(MAX_NOTICE_MSG_IDS.get(key) or "").strip()
        params = {"user_id": str(user_id)}
        payload: dict = {"text": text}
        if keyboard_buttons:
            _log_max_open_chat_button(keyboard_buttons)
            payload["attachments"] = [{
                "type": "inline_keyboard",
                "payload": {"buttons": keyboard_buttons},
            }]

        new_mid = ""
        headers = {"Authorization": MAX_BOT_TOKEN}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{MAX_API_BASE_URL}/messages", params=params, json=payload, headers=headers)
                logger.info(f"MAX send_message → status={r.status_code}, body={r.text[:500]}")
                if r.status_code != 200:
                    logger.error(f"MAX API error (send_message): {r.status_code}, {r.text}")
                    return
                data = r.json() or {}
                msg_obj = data.get("message")
                if isinstance(msg_obj, dict):
                    new_mid = str(msg_obj.get("body", {}).get("mid") or "").strip()
                if not new_mid:
                    # fallback на плоские поля на случай изменения API
                    new_mid = str(data.get("message_id") or data.get("id") or "").strip()
                logger.info(f"MAX send_message new_mid={new_mid!r}")
        except Exception as e:
            logger.error(f"Error sending MAX message: {e}")
            return

        if not new_mid:
            logger.warning(f"MAX send_message: message sent but no message_id in response, key={key}")
            return

        MAX_NOTICE_MSG_IDS[key] = new_mid

        if prev_mid and prev_mid != new_mid:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    dr = await client.delete(
                        f"{MAX_API_BASE_URL}/messages",
                        params={"message_id": prev_mid},
                        headers=headers,
                    )
                    if dr.status_code != 200:
                        logger.info(f"MAX delete_message failed: {dr.status_code}, {dr.text}")
            except Exception as e:
                logger.info(f"Error deleting MAX message: {e}")

async def build_webapp_open_chat_for_other_side(
    *,
    chat: dict,
    sender_role: str,
    db,
    webapp_base_url: str,
):
    """Возвращает каналы и кнопки для уведомления второй стороне."""
    sender_role = (sender_role or "").lower()

    client_internal_id = chat.get("client_user_id")
    if client_internal_id is None:
        logger.warning(f"No client_user_id in chat {chat.get('id')}")
        return None
    client_internal_id = int(client_internal_id)
    client_user = await db.get_user_by_internal_id(client_internal_id)
    if not client_user:
        logger.warning(f"Cannot resolve client users.id={client_internal_id} in chat {chat.get('id')}")
        return None
    client_tg = int(client_user["tg_user_id"]) if client_user.get("tg_user_id") else None

    spec_tg = None
    spec = None
    specialist_id = chat.get("specialist_id")
    if specialist_id:
        spec = await db.get_specialist_by_id(int(specialist_id))
        if spec and spec.get("tg_user_id"):
            spec_tg = int(spec["tg_user_id"])

    # Определяем order_id для кнопок навигации (если это чат по заказу)
    req = (chat.get("request_type") or "").strip().lower()
    cat = str(chat.get("category") or "")
    order_id_for_markup = int(cat) if (req == "order" and cat.isdigit()) else None

    if sender_role == "client":
        to_tg_id = spec_tg
        to_max_id = int(spec["max_user_id"]) if spec and spec.get("max_user_id") else None
        # Проверяем роль специалиста через поле role, а не несуществующее is_director
        token_role = "director" if (spec and str(spec.get("role") or "") == "director") else "agent"
    else:
        to_tg_id = client_tg
        to_max_id = int(client_user["max_user_id"]) if client_user and client_user.get("max_user_id") else None
        token_role = "client"
    result = {
        "telegram": None,
        "max": None,
    }

    if to_tg_id:
        tg_token = await db.create_chat_access_token(
            chat_id=int(chat["id"]),
            role=token_role,
            actor_id=str(to_tg_id),
        )
        tg_url = f"{webapp_base_url.rstrip('/')}/chat?t={tg_token}"
        result["telegram"] = {
            "user_id": int(to_tg_id),
            "reply_markup": webapp_markup(chat["id"], tg_url, token_role, order_id=order_id_for_markup),
        }

    if to_max_id:
        max_token = await db.create_chat_access_token(
            chat_id=int(chat["id"]),
            role=token_role,
            actor_id=str(to_max_id),
        )
        if MAX_BOT_NAME:
            max_url = f"https://max.ru/{MAX_BOT_NAME}?startapp={max_token}"
        else:
            max_url = f"{webapp_base_url.rstrip('/')}/max_entry?payload={max_token}"
        result["max"] = {
            "user_id": int(to_max_id),
            "keyboard": max_webapp_markup(chat["id"], max_url, token_role),
        }

    logger.info(
        "Generated notification targets for chat %s, role %s: telegram=%s, max=%s",
        chat["id"], token_role, bool(result["telegram"]), bool(result["max"])
    )

    return result

async def require_token_for_chat(chat_id: int, token: str):
    logger.debug(f"Validating token for chat {chat_id}")
    row = await db.get_chat_by_token(token)
    if not row:
        # Мягкая самовосстановляющая проверка: пробуем резолвнуть/обновить токен
        # (кнопка могла устареть, а фронт ещё использует старое значение).
        resolved = await db.resolve_chat_access_token(token)
        if resolved:
            row = await db.get_chat_by_token(resolved)

    if not row:
        logger.warning(f"Invalid token for chat {chat_id}")
        raise HTTPException(status_code=401, detail="Invalid token")

    if int(row["chat_id"]) != int(chat_id):
        logger.warning(f"Token mismatch: token for chat {row['chat_id']} used for chat {chat_id}")
        raise HTTPException(status_code=403, detail="Token mismatch")

    if row.get("revoked_at"):
        logger.warning(f"Token revoked for chat {chat_id}")
        raise HTTPException(status_code=401, detail="Token revoked")

    expires = row.get("expires_at")
    if isinstance(expires, datetime):
        if expires < datetime.utcnow():
            logger.warning(f"Token expired for chat {chat_id}")
            raise HTTPException(status_code=401, detail="Token expired")
    
    logger.debug(f"Token validated successfully for chat {chat_id}")
    return row
