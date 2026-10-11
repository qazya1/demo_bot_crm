# bot_agent_chat.py
# Специалист ↔ клиент: меню чатов, переписка (текст+файлы), принятие/отказ, ссылка оплаты, закрытие обращения.
# python-telegram-bot v20+

from __future__ import annotations

import logging
import os
import re
import time
import asyncio
import zlib

logger = logging.getLogger(__name__)

# Трекинг уведомлений клиентам: (max_uid_client, chat_id) -> message_id
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Message,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot_common import send_or_edit, show_main_menu, STATE_IDLE, arm_chat_reply, clear_chat_reply_flags,chat_webapp_button
from tinkoff import init_payment, get_payment_state
from db import (
    list_chat_offers,
    delete_chat_offers,

    get_specialist_by_tg,
    get_specialist_by_max,
    is_partner_specialist,
    set_specialist_phone_verified,
    link_specialist_tg_user,
    request_chat_close,
    reset_chat_close_votes,

    list_chats_for_specialist,
    get_chat_by_id,
    get_chat_messages,
    count_chat_messages,
    add_chat_message,

    create_chat_payment,
    get_payment_by_payment_id,
    try_accept_chat,
    mark_chat_declined,
    list_chats_for_director,
    get_director_chat_counts,
    list_specialists_for_profession,
    get_specialist_by_id,
    get_user_by_internal_id,
    get_or_create_specialist_ref_code,
    get_user,
)
from qr_utils import generate_qr_file_hashed
from max_api import InlineButton, InlineKeyboard
from max_api import MaxBotAPI
from notice_center import (
    NoticeCluster,
    NoticeKind,
    clear_other_notices_for_user_tg,
    delete_notice_cluster_tg,
    delete_notice_tg,
    upsert_notice_max,
    upsert_notice_tg,
)
from webapp_settings import TG_REF_BOT_USERNAME, MAX_BOT_NAME
try:
    from config import MAX_BOT_TOKEN
except ImportError:  # pragma: no cover - fallback for package imports
    from webapp.config import MAX_BOT_TOKEN

MAX_REF_LINK_BOT_NAME = MAX_BOT_NAME

# ===== Conversation states =====
AG_WAIT_PHONE = 7101
AG_MENU_CHATS = 7102
AG_CHAT_VIEW = 7103
AG_WAIT_REPLY = 7104
AG_WAIT_PAY_AMOUNT = 7105
AG_CLOSE_CONFIRM = 7106
AG_LAST_CHAT_MENU_MID_KEY = "_ag_last_chat_menu_mid"

SENDER_CLIENT = 0
SENDER_SPEC = 1

MAX_HISTORY = 100


def status_ru(status: str | None) -> str:
    """Отображение статуса заявки для UI на русском."""
    s = (status or "").strip().lower()
    return {
        "pending": "Ожидает принятия",
        "active": "В работе",
        "test": "Тест",
        "closed": "Закрыто",
        "declined": "Отклонено",
        "consultation": "Консультация",
    }.get(s, status or "—")


async def _notify_client_all_platforms(
    chat: Dict[str, Any],
    *,
    tg_bot,
    text_tg: str,
    tg_reply_markup: InlineKeyboardMarkup | None = None,
    text_max: str | None = None,
    max_keyboard: InlineKeyboard | None = None,
    tg_parse_mode: str | None = None,
) -> None:
    client_internal_id = int(chat.get("client_user_id") or 0)
    if not client_internal_id:
        return

    client_user = await get_user_by_internal_id(client_internal_id)
    if not client_user:
        return

    client_tg_chat_id = int(client_user.get("tg_user_id") or 0) if client_user.get("tg_user_id") else 0
    client_max_user_id = int(client_user.get("max_user_id") or 0) if client_user.get("max_user_id") else 0

    if client_tg_chat_id:
        try:
            await upsert_notice_tg(
                tg_bot=tg_bot,
                tg_chat_id=client_tg_chat_id,
                user_id=client_tg_chat_id,
                kind=NoticeKind.CLIENT_CHAT,
                entity_id=int(chat.get("id") or 0),
                text=text_tg,
                parse_mode=tg_parse_mode,
                reply_markup=tg_reply_markup,
            )
        except Exception as e:
            logger.error("TG notify client %s failed: %s", client_tg_chat_id, e)

    if client_max_user_id and MAX_BOT_TOKEN:
        max_text = text_max or text_tg
        chat_id = int(chat.get("id") or 0)
        api = MaxBotAPI(token=MAX_BOT_TOKEN)
        await api.start()
        try:
            await upsert_notice_max(
                max_api=api,
                max_user_id=int(client_max_user_id),
                kind=NoticeKind.CLIENT_CHAT,
                entity_id=int(chat_id),
                text=max_text,
                keyboard=max_keyboard,
                parse_mode="html" if tg_parse_mode == ParseMode.HTML else None,
            )
        except Exception as e:
            logger.error("MAX notify client %s failed: %s", client_max_user_id, e)
        finally:
            await api.close()


@dataclass
class AgentCtx:
    specialist_id: int
    tg_user_id: int
    profession: int
    role: str = "specialist"



# ----------------- helpers -----------------

def _norm_phone(phone: str) -> str:
    s = re.sub(r"\D+", "", phone or "")
    if s.startswith("8") and len(s) == 11:
        s = "7" + s[1:]
    if s.startswith("9") and len(s) == 10:
        s = "7" + s
    return s


def _is_file(m: Message) -> bool:
    return bool(m.document or m.photo or m.video or m.audio or m.voice or m.video_note or m.animation)


def _kb_phone_request() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Подтвердить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def _short(s: str, n: int = 28) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"

def _kb_dir_sections(counts: dict | None = None, *, allow_client_menu: bool = False) -> InlineKeyboardMarkup:
    counts = counts or {}

    def _get(key):
        d = counts.get(key) or {}
        return (
            int(d.get("pending") or 0),
            int(d.get("active") or 0)+int(d.get("consultation") or 0),
            int(d.get("total") or 0)
        )

    def _fmt(key) -> str:
        p, a, t = _get(key)
        # формат: (🟡3 🟢2) — и видно "наличие", и сколько
        return f" (🟡{p} 🟢{a})" if (p or a) else " (0)"

    rows = [
        [InlineKeyboardButton(f"📌 Все заявки{_fmt('all')}", callback_data="ag_dir_prof:all")],
        [InlineKeyboardButton(f"🧾 Бухгалтерия{_fmt(0)}", callback_data="ag_dir_prof:0")],
        [InlineKeyboardButton(f"🛠 Техподдержка{_fmt(1)}", callback_data="ag_dir_prof:1")],
        [InlineKeyboardButton(f"💼 Услуги{_fmt(2)}", callback_data="ag_dir_prof:2")],
        [InlineKeyboardButton(f"💼 Оборудование консультация{_fmt(3)}", callback_data="ag_dir_prof:3")],
        [InlineKeyboardButton(f"📄 Разрешительная документация{_fmt(4)}", callback_data="ag_dir_prof:4")],
    ]
    if allow_client_menu:
        rows.append([InlineKeyboardButton("🔙 В меню клиента", callback_data="back_to_main")])
    return InlineKeyboardMarkup(rows)



def _kb_chats(chat_rows, header_rows=None, *, allow_client_menu: bool = False):
    rows = []
    if header_rows:
        rows.extend(header_rows)
    for ch in chat_rows:
        client = ch.get("client_name") or f"Клиент {ch.get('client_user_id')}"
        topic = ch.get("category") or ch.get("request_type") or f"Чат #{ch.get('id')}"
        status = ch.get("status")
        badge = "🟢" if status == "active" or status == "consultation" else "🟡" if status == "pending" else "⚪"

        rows.append([
            InlineKeyboardButton(
                f"{badge} {client} — {_short(topic)}",
                callback_data=f"ag_chat:{ch['id']}"
            )
        ])

    rows.append([InlineKeyboardButton("🎯 Мой реферальный QR", callback_data="ag_ref_qr")])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="ag_refresh")])
    if allow_client_menu:
        rows.append([InlineKeyboardButton("🔙 В меню клиента", callback_data="back_to_main")])
    return InlineKeyboardMarkup(rows)


async def cb_ref_qr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except Exception:
            pass

    agent = _get_agent_ctx(update, context)
    if not agent:
        return await agent_entry(update, context)

    data = (q.data or "") if q else ""
    selected_platform = ""
    if ":" in data:
        selected_platform = data.split(":", 1)[1].strip().lower()

    if selected_platform not in ("telegram", "max"):
        await update.effective_chat.send_message(
            "Выберите платформу для реферального QR:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Telegram", callback_data="ag_ref_qr:telegram")],
                [InlineKeyboardButton("MAX", callback_data="ag_ref_qr:max")],
                [InlineKeyboardButton("📂 К заявкам", callback_data="ag_refresh")],
            ]),
        )
        return AG_MENU_CHATS

    spec = await get_specialist_by_id(agent.specialist_id)
    owner_name = (spec or {}).get("name") or f"Специалист #{agent.specialist_id}"
    ref_code = await get_or_create_specialist_ref_code(agent.specialist_id, owner_name=owner_name)
    if not ref_code:
        await update.effective_chat.send_message("Не удалось создать реферальный код. Попробуйте позже.")
        return AG_MENU_CHATS

    if selected_platform == "telegram":
        if not TG_REF_BOT_USERNAME:
            await update.effective_chat.send_message("⚠️ Не настроен TG_REF_BOT_USERNAME для реферальных ссылок.")
            return AG_MENU_CHATS
        referral_url = f"https://t.me/{TG_REF_BOT_USERNAME}?start=ref_{ref_code}"
        qr_namespace = "tg_specialist_ref"
    else:
        if not MAX_REF_LINK_BOT_NAME:
            await update.effective_chat.send_message("⚠️ Не настроен MAX_BOT_NAME для реферальных ссылок.")
            return AG_MENU_CHATS
        referral_url = f"https://max.ru/{MAX_REF_LINK_BOT_NAME}?start=ref_{ref_code}"
        qr_namespace = "max_specialist_ref"

    back_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Вернуться к заявкам", callback_data="ag_refresh")],
    ])
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        qr_dir = os.path.join(base_dir, "images", "qrs")
        os.makedirs(qr_dir, exist_ok=True)
        qr_path = generate_qr_file_hashed(referral_url, out_dir=qr_dir, namespace=qr_namespace)
        with open(qr_path, "rb") as f:
            await update.effective_chat.send_photo(
                photo=f,
                caption=(
                    "🎯 Ваш реферальный QR для раздела «Мои заявки».\n"
                    "Поделитесь им с клиентом — ссылка активирует ref-код автоматически.\n\n"
                    f"Ссылка: {referral_url}"
                ),
                reply_markup=back_kb,
            )
    except Exception:
        await update.effective_chat.send_message(
            f"Ваша реферальная ссылка:\n{referral_url}",
            reply_markup=back_kb,
            disable_web_page_preview=True,
        )
    if q and q.message:
        try:
            await q.message.delete()
        except Exception:
            pass
    return AG_MENU_CHATS


async def _kb_chat_menu(update: Update, chat_id: int, status: str | None = None, *, readonly: bool = False, allow_client_menu: bool = False) -> InlineKeyboardMarkup:
    st = (status or "").strip().lower()

    if readonly or st == "closed":
        return InlineKeyboardMarkup([
            [await chat_webapp_button(
                chat_id=chat_id,
                role="agent",
                actor_id=update.effective_user.id, text="💬 Ответить")],
            [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
        ])

    if allow_client_menu:
        if st == "consultation":
            return InlineKeyboardMarkup([
                [await chat_webapp_button(
                    chat_id=chat_id,
                    role="director",
                    actor_id=update.effective_user.id, text="💬 Ответить")],
                [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
                [InlineKeyboardButton("🔙 В меню клиента", callback_data="back_to_main")]
            ])
        return InlineKeyboardMarkup([
            [await chat_webapp_button(
                chat_id=chat_id,
                role="director",
                actor_id=update.effective_user.id, text="💬 Ответить")],
            [InlineKeyboardButton("✅ Завершить обращение", callback_data=f"ag_close:{chat_id}")],
            [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
            [InlineKeyboardButton("🔙 В меню клиента", callback_data="back_to_main")],
            [InlineKeyboardButton("🔁 Сменить специалиста", callback_data=f"ag_reassign:{chat_id}")],
        ])
    else:
        if st == "consultation":
            return InlineKeyboardMarkup([
                [await chat_webapp_button(
                    chat_id=chat_id,
                    role="director",
                    actor_id=update.effective_user.id, text="💬 Ответить")],
                [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
            ])
        return InlineKeyboardMarkup([
            [await chat_webapp_button(
                chat_id=chat_id,
                role="agent",
                actor_id=update.effective_user.id, text="💬 Ответить")],
            [InlineKeyboardButton("✅ Завершить обращение", callback_data=f"ag_close:{chat_id}")],
            [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
        ])

async def _kb_close_confirm(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data=f"ag_close_yes:{chat_id}")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"ag_close_no:{chat_id}")],
    ])


def _get_agent_ctx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[AgentCtx]:
    d = context.user_data.get("agent_ctx")
    if not isinstance(d, dict):
        return None
    try:
        return AgentCtx(
            specialist_id=int(d["specialist_id"]),
            tg_user_id=int(d["tg_user_id"]),
            profession=int(d["profession"]),
            role=str(d.get("role") or "specialist"),
        )
    except Exception as e:
        print(e)
        return None


def _set_agent_ctx(context: ContextTypes.DEFAULT_TYPE, row: Dict[str, Any], tg_user_id: int) -> None:
    profession = int(row.get("profession") or 0)
    role = row.get("role") or "specialist"
    # profession=2 всегда director, даже если role в БД ещё не обновлён
    if profession == 2 or int(row.get("is_director") or 0) == 1:
        role = "director"
    context.user_data["agent_ctx"] = {
        "specialist_id": int(row["id"]),
        "tg_user_id": int(tg_user_id),
        "profession": profession,
        "role": role,
    }


async def _clear_specialist_service_notices_for_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Best-effort: при работе со служебным callback чистим остальные pending-notices специалиста."""
    q = update.callback_query
    if not q:
        return
    tg_uid = int(update.effective_user.id)
    current_mid = int(q.message.message_id) if q.message else None
    clusters = [
        NoticeCluster.SPECIALIST_PENDING_NOTICES,
        NoticeCluster.SPECIALIST_CHAT_NOTICES,
    ]
    try:
        current_cluster = None
        for cluster in clusters:
            matched = await clear_other_notices_for_user_tg(
                tg_bot=context.application.bot,
                tg_chat_id=tg_uid,
                user_id=tg_uid,
                cluster=cluster,
                current_message_id=current_mid,
            )
            if matched:
                current_cluster = cluster

        for cluster in clusters:
            if cluster == current_cluster:
                continue
            await delete_notice_cluster_tg(
                tg_bot=context.application.bot,
                tg_chat_id=tg_uid,
                user_id=tg_uid,
                cluster=cluster,
            )
    except Exception:
        logger.debug("best-effort specialist notice cleanup failed for %s", tg_uid, exc_info=True)


async def _render_chat_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    agent = _get_agent_ctx(update, context)
    if not agent:
       return await agent_entry(update, context)
    try:
        app = context.application
        mute = app.bot_data.get("spec_mute_until_ts")
        if not isinstance(mute, dict):
            mute = {}
            app.bot_data["spec_mute_until_ts"] = mute
        # заглушаем на 120 секунд
        mute[int(agent.tg_user_id)] = time.time() + 120
    except Exception:
        pass
    try:
        await delete_notice_tg(
            tg_bot=context.application.bot,
            tg_chat_id=int(agent.tg_user_id),
            user_id=int(agent.tg_user_id),
            kind=NoticeKind.SPECIALIST_PENDING,
            entity_id=int(agent.tg_user_id),
        )
    except Exception:
        logger.debug("best-effort pending notice cleanup failed for specialist %s", agent.tg_user_id, exc_info=True)

    context.user_data.pop("ag_reply_chat_id", None)
    context.user_data.pop("ag_reply_until", None)

    async def _render_list_screen(text_: str, kb_: InlineKeyboardMarkup) -> None:
        prev_mid = int(context.user_data.get(AG_LAST_CHAT_MENU_MID_KEY) or 0)
        used_mid: int | None = None

        q = update.callback_query
        if q and q.message:
            try:
                await q.answer(cache_time=0)
            except Exception:
                pass
            try:
                current_text = getattr(q.message, "text", None)
                current_kb = getattr(q.message, "reply_markup", None)
                if current_text == text_ and current_kb == kb_:
                    used_mid = int(q.message.message_id)
                else:
                    await q.message.edit_text(text_, reply_markup=kb_, parse_mode=None, disable_web_page_preview=True)
                    used_mid = int(q.message.message_id)
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    used_mid = int(q.message.message_id)
                else:
                    sent = await update.effective_chat.send_message(
                        text_,
                        reply_markup=kb_,
                        parse_mode=None,
                        disable_web_page_preview=True,
                    )
                    used_mid = int(sent.message_id)
            except Exception:
                sent = await update.effective_chat.send_message(
                    text_,
                    reply_markup=kb_,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                used_mid = int(sent.message_id)
        else:
            sent = await update.effective_chat.send_message(
                text_,
                reply_markup=kb_,
                parse_mode=None,
                disable_web_page_preview=True,
            )
            used_mid = int(sent.message_id)

        if used_mid:
            context.user_data[AG_LAST_CHAT_MENU_MID_KEY] = used_mid
        if prev_mid and used_mid and prev_mid != used_mid:
            try:
                await context.application.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=prev_mid,
                )
            except Exception:
                logger.debug("best-effort delete previous TG specialist chat menu failed: mid=%s", prev_mid, exc_info=True)

    text = "📂 Ваши чаты / заявки:\n\n🟡 — ожидают принятия\n🟢 — ваши активные\n\nВыберите:"

    # обычный специалист — старое поведение
    if agent.role != "director":
        chats = await list_chats_for_specialist(agent.specialist_id)
        await _render_list_screen(text, _kb_chats(chats, allow_client_menu=False))
        return AG_MENU_CHATS

    # директор — сначала выбор отдела
    sel = context.user_data.get("ag_dir_prof")
    if not sel:
        try:
            counts = await get_director_chat_counts()
        except Exception:
            counts = {}
        await _render_list_screen("Выберите раздел заявок:", _kb_dir_sections(counts, allow_client_menu=True))
        return AG_MENU_CHATS

    profession = None if sel == "all" else int(sel)
    chats = await list_chats_for_director(profession=profession)

    kb = _kb_chats(
        chats,
        header_rows=[[InlineKeyboardButton("⬅️ К отделам", callback_data="ag_dir_sections")]],
        allow_client_menu=True,
    )
    await _render_list_screen(text, kb)

    return AG_MENU_CHATS


async def _show_chat_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    chat = await get_chat_by_id(chat_id)
    if not chat:
        await send_or_edit(update, "Чат не найден.", None, parse_mode=None)
        return AG_MENU_CHATS
    agent = _get_agent_ctx(update, context)

    title = chat.get("client_name") or f"Клиент {chat.get('client_user_id')}"

    phone = (chat.get("client_phone") or "").strip()
    inn = (chat.get("client_inn") or "").strip()
    we_added = bool(chat.get("client_we_added"))
    is_director = bool(agent and agent.role == "director")
    client_info_lines = [
        ('Наш клиент' if we_added else 'Неизвестный клиент')
    ]
    if is_director:
        client_info_lines.append(f"Телефон: <b>{phone}</b>" if phone else "Телефон: <b>—</b>")
        client_info_lines.append(f"ИНН: <b>{inn}</b>" if inn else "ИНН: <b>—</b>")
    else:
        client_info_lines.append("Телефон: <b>указан</b>" if phone else "Телефон: <b>не указан</b>")
        client_info_lines.append("ИНН: <b>указан</b>" if inn else "ИНН: <b>не указан</b>")

    client_info_block = "\n" + "\n".join(client_info_lines) + "\n"

    text = (
        f"💬 <b>Чат:</b> {title}\n"
        f"Тип: <b>{chat.get('request_type') or '—'}</b>\n"
        f"Категория: <b>{chat.get('category') or '—'}</b>\n"
        f"Статус: <b>{status_ru(chat.get('status'))}</b>"
        f"{client_info_block}\n"
        "⬇️ Переписка открывается в мини-приложении:\n"
        "нажмите «Ответить»."
    )

    await send_or_edit(
        update,
        text,
        (await _kb_chat_menu(update, chat_id, chat.get("status"), allow_client_menu=(agent.role == "director"))),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return AG_MENU_CHATS


# =========================
# ENTRYPOINT: запуск для специалиста
# =========================
async def agent_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    bot_main уже проверил, что username = специалист.
    Здесь мы:
      1) поднимаем схему
      2) ищем специалиста по tg_user_id/username
      3) проверяем телефон через contact
    """

    tg_user = update.effective_user
    tg_uid = int(tg_user.id)
    username = (tg_user.username or "").strip()

    spec = await get_specialist_by_tg(tg_user_id=tg_uid, tg_username=username)
    logger.info(
        "[SPEC-TG] agent_entry: uid=%s uname=%r → spec_id=%s profession=%s role=%s phone_verified=%s is_active=%s",
        tg_uid, username,
        (spec or {}).get("id"), (spec or {}).get("profession"),
        (spec or {}).get("role"), (spec or {}).get("phone_verified"),
        (spec or {}).get("is_active"),
    )
    if not spec:
        # Fallback: specialist may be linked only via max_user_id (registered via MAX bot first)
        try:
            user_row = await get_user(str(tg_uid))
            max_uid = int((user_row or {}).get("max_user_id") or 0)
            if max_uid:
                spec = await get_specialist_by_max(max_user_id=max_uid)
                logger.info("[SPEC-TG] agent_entry max_uid fallback: uid=%s max_uid=%s spec_id=%s", tg_uid, max_uid, (spec or {}).get("id"))
        except Exception:
            pass

    if not spec or is_partner_specialist(spec):
        logger.warning("[SPEC-TG] agent_entry: no spec or is_partner → show_main_menu: uid=%s", tg_uid)
        context.user_data.pop("_is_specialist", None)
        context.user_data.pop("agent_ctx", None)
        return await show_main_menu(update, context)

    # ЖЁСТКО привязываем tg_user_id при входе спеца
    try:
        await link_specialist_tg_user(int(spec["id"]), tg_uid, username or None)
        # обновим локально, чтобы дальше не было NULL
        spec["tg_user_id"] = tg_uid
        if username:
            spec["tg_username"] = username
    except Exception as e:
        print(e)
        pass

    _set_agent_ctx(context, spec, tg_uid)

    # если уже в БД отмечено phone_verified=1 — сразу в меню чатов
    # НО: если tg_user_id ещё не привязан (NULL) — всё равно просим contact, чтобы жёстко привязать Telegram-аккаунт.

    need_link = (spec.get("tg_user_id") is None) or (int(spec.get("tg_user_id") or 0) != tg_uid)
    is_verified = int(spec.get("phone_verified") or 0) == 1
    logger.info(
        "[SPEC-TG] agent_entry decision: uid=%s spec_id=%s need_link=%s is_verified=%s → %s",
        tg_uid, spec.get("id"), need_link, is_verified,
        "render_chat_list" if is_verified else "AG_WAIT_PHONE",
    )

    if is_verified:
        # если телефон уже подтвержден — просто перепривяжем телеграм и пускаем
        try:
            await link_specialist_tg_user(int(spec["id"]), tg_uid, username or None)
        except Exception as e:
            logger.warning("[SPEC-TG] agent_entry link_specialist_tg_user failed: %s", e)
        return await _render_chat_list(update, context)

    # иначе просим contact (либо не верифицирован, либо tg_user_id не привязан/сменился)
    await send_or_edit(
        update,
        "Для входа в режим специалиста подтвердите номер телефона кнопкой ниже.",
        None,
        parse_mode=None,
    )
    await update.effective_chat.send_message(
        "Нажмите кнопку «Подтвердить номер телефона».",
        reply_markup=_kb_phone_request(),
    )
    return AG_WAIT_PHONE


# =========================
# 1) Подтверждение телефона
# =========================
async def on_agent_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    agent = _get_agent_ctx(update, context)
    logger.info("[SPEC-TG] on_agent_contact: uid=%s agent_ctx=%s", (update.effective_user or {}).get("id"), agent)
    if not agent:
        logger.warning("[SPEC-TG] on_agent_contact: no agent_ctx → show_main_menu")
        return await show_main_menu(update, context)

    m = update.effective_message
    if not (m and m.contact and m.contact.phone_number):
        await update.effective_chat.send_message(
            "Пришлите контакт кнопкой, пожалуйста.",
            reply_markup=_kb_phone_request(),
        )
        return AG_WAIT_PHONE

    # ищем спеца снова из БД (актуальные данные)
    spec = await get_specialist_by_tg(
        tg_user_id=agent.tg_user_id,
        tg_username=update.effective_user.username or "",
    )
    if not spec or is_partner_specialist(spec):
        await update.effective_chat.send_message("Режим специалиста недоступен для этого аккаунта.")
        return await show_main_menu(update, context)

    entered = _norm_phone(m.contact.phone_number)
    expected = _norm_phone(spec.get("phone") or "")

    if not expected:
        await update.effective_chat.send_message(
            "Для вашего аккаунта не задан номер телефона в базе. Обратитесь к администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await show_main_menu(update, context)

    if entered != expected:
        await update.effective_chat.send_message(
            "Номер телефона не совпал. Открываю обычное меню клиента. Обратитесь к администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await show_main_menu(update, context)

    # ок — помечаем verified и привязываем tg_user_id/username
    await set_specialist_phone_verified(int(spec["id"]), True)
    await link_specialist_tg_user(
        int(spec["id"]),
        agent.tg_user_id,
        update.effective_user.username or None,
    )

    await update.effective_chat.send_message("✅ Телефон подтверждён.", reply_markup=ReplyKeyboardRemove())
    return await _render_chat_list(update, context)


# =========================
# 3) Меню чатов и просмотр чата
# =========================
async def cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)

    # если agent_ctx не поднят (например, специалист ушёл в меню/перезапуск) —
    # пробуем восстановиться и войти как специалист
    agent = _get_agent_ctx(update, context)
    if not agent:
        return await agent_entry(update, context)

    return await _render_chat_list(update, context)

async def cb_dir_prof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    # ожидаем форматы: ag_dir_prof:all | ag_dir_prof:<profession_id>
    prof = (q.data or "").split(":", 1)[1]
    context.user_data["ag_dir_prof"] = prof
    return await _render_chat_list(update, context)


async def cb_dir_sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    context.user_data.pop("ag_dir_prof", None)
    return await _render_chat_list(update, context)


async def cb_back_to_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("ag_reply_chat_id", None)
    context.user_data.pop("ag_reply_until", None)
    q = update.callback_query
    await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)
    return await _render_chat_list(update, context)


async def cb_open_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)


    # ag_chat:<chat_id>
    try:
        chat_id = int((q.data or "").split(":", 1)[1])
    except Exception:
        await q.edit_message_text("Некорректный идентификатор чата.")
        return AG_MENU_CHATS

    chat = await get_chat_by_id(chat_id)
    if not chat:
        await q.edit_message_text("Чат не найден.")
        return AG_MENU_CHATS

    agent = _get_agent_ctx(update, context)
    if not agent:
        return await agent_entry(update, context)

    st = (chat.get("status") or "").strip().lower()
    chat_spec_id = int(chat.get("specialist_id") or 0)

    # 1) Если заявка закрыта — никто туда ничего не пишет.
    if st == "closed":
        await q.edit_message_text(
            "🔒 Обращение уже закрыто. Ответить в этот чат нельзя.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
            ]),
        )
        return AG_CHAT_VIEW

    # 2) Если заявка открыта/в очереди — директор может писать туда.
    # НО: по заявкам «Услуги» директор — основной исполнитель, поэтому должен ПРИНЯТЬ заявку,
    # иначе client->reply не сможет уйти (specialist_id останется NULL).
    if agent.role == "director":
        if st not in ("active", "pending", "consultation"):
            await q.edit_message_text(
                "⚠️ Нельзя отвечать в этот чат (статус не позволяет).",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
                ]),
            )
            return AG_CHAT_VIEW

        prof = int(chat.get("profession") or 0)
        if prof == 2 and st == "pending" and chat_spec_id == 0:
            await q.edit_message_text(
                "Заявка по «Услугам» ещё не принята.\n"
                "Нажмите «Принять», чтобы начать переписку.",
                reply_markup=InlineKeyboardMarkup([
                    [await chat_webapp_button(
                        chat_id=chat_id,
                        role="director",
                        actor_id=update.effective_user.id, text="💬 Ответить")],
                    [InlineKeyboardButton("✅ Принять", callback_data=f"ag_offer_yes:{chat_id}")],
                    [InlineKeyboardButton("❌ Отклонить", callback_data=f"ag_offer_no:{chat_id}")],
                    [InlineKeyboardButton("⬅️ К списку", callback_data="ag_back_to_chats")],
                ]),
            )
            return AG_CHAT_VIEW

        context.user_data["ag_reply_chat_id"] = chat_id
        context.user_data["ag_reply_until"] = int(time.time()) + 10 * 60
        return await _show_chat_by_id(update, context, chat_id)

    # 3) Обычный специалист может писать только если он принял заявку (закреплена за ним).
    if st == "pending" and chat_spec_id == 0:
        await q.edit_message_text(
            "Заявка ещё не принята. Примите её, чтобы начать переписку.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Принять", callback_data=f"ag_offer_yes:{chat_id}")],
                [InlineKeyboardButton("❌ Отклонить", callback_data=f"ag_offer_no:{chat_id}")],
                [InlineKeyboardButton("⬅️ К списку", callback_data="ag_back_to_chats")],
            ]),
        )
        return AG_CHAT_VIEW

    if chat_spec_id != agent.specialist_id:
        await q.edit_message_text(
            "⚠️ Эта заявка не закреплена за вами (в работе у другого специалиста или уже завершена).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
            ]),
        )
        return AG_CHAT_VIEW

    # Разрешаем отвечать в active (и на всякий случай в pending, если БД держит такой статус после принятия).
    if st not in ("active", "pending", "consultation"):
        await q.edit_message_text(
            "⚠️ Нельзя отвечать в этот чат (статус не позволяет).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
            ]),
        )
        return AG_CHAT_VIEW

    return await _show_chat_by_id(update, context, chat_id)


# =========================
# 5) Принять/отказаться от клиента (реальные обработчики)
# =========================
def _kb_after_send(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Завершить обращение", callback_data=f"ag_close:{chat_id}")],
        [InlineKeyboardButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")]
    ])


async def cb_offer_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)

    agent = _get_agent_ctx(update, context)
    if not agent:
        # Восстанавливаем specialist-контекст, если кнопка нажата из "холодной" сессии
        # (например, после рестарта бота или без входа в раздел специалиста).
        await agent_entry(update, context)
        agent = _get_agent_ctx(update, context)
        if not agent:
            return await show_main_menu(update, context)

    chat_id = int((q.data or "").split(":")[1])
    chat = await get_chat_by_id(chat_id)
    if not chat:
        await update.effective_chat.send_message(
            "⚠️ Чат не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить список", callback_data="ag_refresh")],
            ]),
        )
        return AG_MENU_CHATS

    ok = await try_accept_chat(chat_id, agent.specialist_id)
    if not ok:
        # Удаляем stale notice с кнопками, если заявку уже принял другой специалист.
        try:
            if q and q.message:
                await context.application.bot.delete_message(
                    chat_id=int(q.message.chat_id),
                    message_id=int(q.message.message_id),
                )
        except Exception:
            logger.debug("TG stale specialist notice delete failed", exc_info=True)
        # ❗ не редактируем исходное сообщение
        await update.effective_chat.send_message(
            "⚠️ Эту заявку уже принял другой специалист или она закрыта.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить список", callback_data="ag_refresh")],
            ]),
        )
        return AG_MENU_CHATS


    # ✅ кто-то принял заявку → убираем inline-меню "Принять/Отклонить" у всех специалистов
    try:
        offers = await list_chat_offers(chat_id)
        for off in offers:
            try:
                await context.application.bot.edit_message_reply_markup(
                    chat_id=int(off["tg_chat_id"]),
                    message_id=int(off["message_id"]),
                    reply_markup=None,
                )
            except Exception as e:
                print(e)
                pass
        await delete_chat_offers(chat_id)
    except Exception as e:
        print(e)
        pass

    try:
        await _notify_client_all_platforms(
            chat,
            tg_bot=context.application.bot,
            text_tg="Ваша заявка принята.\nМожете писать сюда.",
            tg_parse_mode=ParseMode.HTML,
            tg_reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("К заявке", callback_data=f"cc_mychat:{chat_id}")],
                [InlineKeyboardButton("📂 Мои обращения", callback_data="cc_my_chats")],
                [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
            ]),
            text_max="✅ Ваша заявка принята. Можете писать в чат.",
            max_keyboard=InlineKeyboard(rows=[
                [InlineButton("К заявке", callback_data=f"cc_mychat:{chat_id}")],
                [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
                [InlineButton("🔙 В меню", callback_data="back_to_main")],
            ]),
        )
    except Exception as e:
        print(e)
        pass

    # ❗ не редактируем исходное сообщение — просто присылаем новое
    await update.effective_chat.send_message(
        "✅ Вы приняли заявку.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Ответить", callback_data=f"ag_chat:{chat_id}")],
            [InlineKeyboardButton("📂 К списку чатов", callback_data="ag_refresh")],
        ]),
    )
    return AG_WAIT_REPLY


from db import (
    list_chat_offers,
    delete_chat_offers,

    mark_chat_declined,
    count_declines_for_chat,
    count_active_specialists_for_profession,
    get_least_busy_specialist_for_profession,
    force_assign_chat,
)

async def cb_offer_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)

    agent = _get_agent_ctx(update, context)
    if not agent:
        # Восстанавливаем specialist-контекст для нажатий из входящих офферов.
        await agent_entry(update, context)
        agent = _get_agent_ctx(update, context)
        if not agent:
            return await show_main_menu(update, context)
    
    
    chat_id = int((q.data or "").split(":")[1])
    chat = await get_chat_by_id(chat_id)
    if not chat:
        await update.effective_chat.send_message(
            "⚠️ Чат не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить список", callback_data="ag_refresh")],
            ]),
        )
        return AG_MENU_CHATS

    await mark_chat_declined(chat_id, agent.specialist_id)

    total_specs = await count_active_specialists_for_profession(int(chat.get("profession") or agent.profession))
    declined = await count_declines_for_chat(chat_id)

    if total_specs > 0 and declined >= total_specs:
        pick = await get_least_busy_specialist_for_profession(int(chat.get("profession") or agent.profession))
        if pick and (await force_assign_chat(chat_id, int(pick["id"]))):
            # назначенному спецу
            try:
                await upsert_notice_tg(
                    tg_bot=context.application.bot,
                    tg_chat_id=int(pick["tg_user_id"]),
                    user_id=int(pick["tg_user_id"]),
                    kind=NoticeKind.SPECIALIST_CHAT,
                    entity_id=int(chat_id),
                    text="⚠️ Все специалисты отказались — заявка назначена вам автоматически.\nОткройте чат и ответьте клиенту.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Ответить", callback_data=f"ag_chat:{chat_id}")],
                    ]),
                )
            except Exception as e:
                print(e)
                pass

            try:
                await _notify_client_all_platforms(
                    chat,
                    tg_bot=context.application.bot,
                    text_tg="✅ Ваше обращение принято. Можете писать сюда.",
                    tg_reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📂 Мои обращения", callback_data="cc_my_chats")],
                        [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
                    ]),
                    text_max="✅ Ваше обращение принято. Можете писать в чат.",
                    max_keyboard=InlineKeyboard(rows=[
                        [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
                        [InlineButton("🔙 В меню", callback_data="back_to_main")],
                    ]),
                )
            except Exception as e:
                print(e)
                pass

            await update.effective_chat.send_message(
                "Вы отказались. Все отказались — заявка назначена специалисту автоматически.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📂 К списку чатов", callback_data="ag_refresh")],
                ]),
            )
            return AG_MENU_CHATS

    await update.effective_chat.send_message(
        "Вы отказались. Заявка остаётся в очереди — её может принять другой специалист.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 К списку чатов", callback_data="ag_refresh")],
        ]),
    )
    return AG_MENU_CHATS

# =========================
# 8) Завершить обращение (с подтверждением)
# =========================
async def cb_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)
    
    chat_id = int((q.data or "").split(":")[1])
    await q.edit_message_text(
        "Вы точно уверены, что все работы по обращению выполнены?",
        reply_markup= await _kb_close_confirm(chat_id),
    )
    return AG_CLOSE_CONFIRM


async def cb_close_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    chat_id = int((q.data or "").split(":")[1])
    return await _show_chat_by_id(update, context, chat_id)

async def cb_close_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)
    chat_id = int((q.data or "").split(":")[1])

    chat = await get_chat_by_id(chat_id)
    if not chat:
        await q.edit_message_text("Чат не найден.")
        return await _render_chat_list(update, context)

    # новая логика: специалист даёт согласие, а чат закрывается только если клиент тоже согласился
    res = await request_chat_close(chat_id, by="agent")
    if not res.get("ok"):
        await q.edit_message_text("Не удалось обработать запрос. Попробуйте ещё раз.")
        return await _render_chat_list(update, context)

    if res.get("just_closed"):
        await add_chat_message(chat_id, SENDER_SPEC, "✅ Обращение завершено по согласию обеих сторон")

        try:
            await _notify_client_all_platforms(
                chat,
                tg_bot=context.application.bot,
                text_tg=(
                    f"✅ Обращение <b>{chat.get('category') or '—'}</b> завершено по согласию обеих сторон.\n\n"
                    "Если нужна помощь снова — создайте новое обращение через меню."
                ),
                tg_parse_mode=ParseMode.HTML,
                tg_reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📂 Мои обращения", callback_data="cc_my_chats")],
                ]),
                text_max=(
                    f"✅ Обращение {chat.get('category') or '—'} завершено по согласию обеих сторон.\n\n"
                    "Если нужна помощь снова — создайте новое обращение через меню."
                ),
                max_keyboard=InlineKeyboard(rows=[
                    [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
                ]),
            )
        except Exception as e:
            print(e)
            pass

        await q.edit_message_text("✅ Чат закрыт (подтверждено обеими сторонами).")
        return await _render_chat_list(update, context)

    # клиент ещё не подтвердил — отправляем ему запрос
    await add_chat_message(chat_id, SENDER_SPEC, "🔒 Запрашиваем завершение обращения (ожидаем клиента)")
    try:
        await _notify_client_all_platforms(
            chat,
            tg_bot=context.application.bot,
            text_tg=(
                "🔒 Мы предлагаем завершить обращение.\n"
                "Чат закроется только если вы подтвердите."
            ),
            tg_reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить завершение", callback_data=f"cc_close:{chat_id}")],
                [InlineKeyboardButton("↩️ Продолжить общение", callback_data=f"cc_close_no:{chat_id}")],
                [InlineKeyboardButton("📂 Мои обращения", callback_data="cc_my_chats")],
            ]),
            text_max="🔒 Мы предлагаем завершить обращение.\nЧат закроется только если вы подтвердите.",
            max_keyboard=InlineKeyboard(rows=[
                [InlineButton("✅ Подтвердить завершение", callback_data=f"cc_close:{chat_id}")],
                [InlineButton("↩️ Продолжить общение", callback_data=f"cc_close_no:{chat_id}")],
                [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
            ]),
        )
    except Exception as e:
        print(e)
        pass

    await q.edit_message_text(
        "🔒 Запрос на завершение отправлен клиенту.\nОжидаем подтверждения клиента.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Ответить", callback_data=f"ag_chat:{chat_id}")]]),
    )
    return AG_CHAT_VIEW


async def cb_close_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Специалист отклоняет/отменяет завершение (сбрасываем флаги и продолжаем чат)."""
    q = update.callback_query
    await q.answer()
    await _clear_specialist_service_notices_for_callback(update, context)

    chat_id = int((q.data or "").split(":")[1])
    chat = await get_chat_by_id(chat_id)
    if not chat:
        await q.edit_message_text("Чат не найден.")
        return await _render_chat_list(update, context)

    if str(chat.get("status")) == "closed":
        await q.edit_message_text("Чат уже закрыт.")
        return await _render_chat_list(update, context)

    await reset_chat_close_votes(chat_id)
    await add_chat_message(chat_id, SENDER_SPEC, "↩️ Мы отменили завершение (продолжаем общение)")

    try:
        await _notify_client_all_platforms(
            chat,
            tg_bot=context.application.bot,
            text_tg="↩️ Мы не подтвердили завершение. Продолжаем общение.",
            tg_reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
            ]),
            text_max="↩️ Мы не подтвердили завершение. Продолжаем общение.",
            max_keyboard=InlineKeyboard(rows=[
                [InlineButton("🔙 В меню", callback_data="back_to_main")],
            ]),
        )
    except Exception as e:
        print(e)
        pass

    await q.edit_message_text(
        "↩️ Закрытие отменено. Продолжаем чат.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Ответить", callback_data=f"ag_chat:{chat_id}")]]),
    )
    return AG_WAIT_REPLY

# =========================
# 7) Уведомление специалиста об успешной оплате (хук из client side)
# =========================
async def notify_agent_payment_success(context: ContextTypes.DEFAULT_TYPE, payment_id: str) -> None:
    """
    Вызывается из bot_client_chat при подтверждении оплаты.
    """
    pay = await get_payment_by_payment_id(payment_id)
    if not pay:
        return
    chat = await get_chat_by_id(int(pay["chat_id"]))
    if not chat:
        return

    # получим текущего спеца через чат->specialist_id
    # в list_chats_for_specialist мы уже джоиним tg_user_id, но здесь возьмём из БД через get_specialist_by_tg можно не удобно
    # поэтому в pay храним specialist_tg_user_id (см. db функции ниже)
    tg_uid = pay.get("specialist_tg_user_id")
    if not tg_uid:
        return

    try:
        notice_entity_id = zlib.crc32(str(payment_id).encode("utf-8")) & 0x7FFFFFFF
        await upsert_notice_tg(
            tg_bot=context.application.bot,
            tg_chat_id=int(tg_uid),
            user_id=int(tg_uid),
            kind=NoticeKind.SPECIALIST_CHAT,
            entity_id=notice_entity_id,
            text=f"✅ Оплата подтверждена (payment_id={payment_id}) по чату #{chat['id']}",
        )
    except Exception as e:
        print(e)
        pass

async def cb_reassign_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    agent = _get_agent_ctx(update, context)
    if not agent or agent.role != "director":
        return AG_MENU_CHATS

    chat_id = int(q.data.split(":")[1])
    chat = await get_chat_by_id(chat_id)
    if not chat:
        await q.edit_message_text("Чат не найден.")
        return AG_MENU_CHATS

    specs = await list_specialists_for_profession(int(chat["profession"]))

    rows = []
    for s in specs:
        rows.append([
            InlineKeyboardButton(
                f"👤 {s['name']}",
                callback_data=f"ag_reassign_to:{chat_id}:{s['id']}"
            )
        ])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"ag_chat:{chat_id}")])

    await q.edit_message_text(
        "Выберите специалиста для переназначения:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AG_MENU_CHATS

async def cb_reassign_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    _, chat_id, spec_id = q.data.split(":")
    chat_id = int(chat_id)
    spec_id = int(spec_id)

    ok = await force_assign_chat(chat_id, spec_id)
    if not ok:
        await q.edit_message_text("Не удалось переназначить чат.")
        return AG_MENU_CHATS

    spec = await get_specialist_by_id(spec_id)

    # уведомляем нового специалиста
    if spec and spec.get("tg_user_id"):
        try:
            await upsert_notice_tg(
                tg_bot=context.application.bot,
                tg_chat_id=int(spec["tg_user_id"]),
                user_id=int(spec["tg_user_id"]),
                kind=NoticeKind.SPECIALIST_CHAT,
                entity_id=int(chat_id),
                text="📨 Вам назначено новое обращение директором.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Ответить", callback_data=f"ag_chat:{chat_id}")]
                ]),
            )
        except Exception:
            pass

    await q.edit_message_text(
        f"✅ Чат переназначен специалисту: {spec['name'] if spec else spec_id}"
    )
    return AG_MENU_CHATS
