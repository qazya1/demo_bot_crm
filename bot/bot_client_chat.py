# bot_client_chat.py

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)
from typing import Optional, Dict, Any, List
from telegram.helpers import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot_common import (
    send_or_edit,
    show_main_menu,
    STATE_IDLE,
    arm_chat_reply,
    clear_chat_reply_flags,
    is_specialist_user,
    chat_webapp_button,
    send_temporary_message,
)
from notice_center import (
    NoticeCluster,
    NoticeKind,
    clear_other_notices_for_user_tg,
    clear_user_notices_tg,
    delete_notice_cluster_tg,
    delete_notice_tg,
    upsert_notice_tg,
)
from tinkoff import get_payment_state

from bot_agent_chat import notify_agent_payment_success

import os
import sys

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
from db import (
    get_user,
    add_chat_offer,
    is_user_verified,
    resolve_internal_user_id,

    get_active_chat_for_client,
    get_chat_by_id,
    request_chat_close,
    reset_chat_close_votes,
    add_chat_message,
    get_chat_messages,
    count_chat_messages,
    set_payment_status_by_payment_id,
    list_chats_for_client,
    get_specialist_by_id,
    get_director_specialist,
    get_nav_snapshot,
    PARTNER_DIRECTOR_REQUEST_TYPE,
    ensure_partner_entrypoint_chat,
    resolve_partner_identity_by_phone,
)

from db import create_chat_access_token, add_chat_message
from bot_common import WEBAPP_URL
from cross_notify import notify_specialist_max, notify_specialist_all_platforms
import httpx


# ===== Локальные состояния ConversationHandler =====
CC_WAIT_TYPE = 6101
CC_WAIT_CATEGORY = 6102
CC_WAIT_PROMO_CHOICE = 6103
CC_WAIT_PROMO_INPUT = 6104
CC_WAIT_CONTENT = 6105
CC_WAIT_REPLY = 6106

SENDER_CLIENT = 0
SENDER_SPEC = 1

PENDING_REPLY_DELAY = 120  # секунд до отправки автоматического сообщения клиенту
PENDING_REPLY_TEXT = "👋 Ваше обращение принято! Специалист скоро с вами свяжется."


async def _schedule_pending_chat_reply(
    bot,
    tg_chat_id: int,
    support_chat_id: int,
    delay: int = PENDING_REPLY_DELAY,
) -> None:
    """Через delay секунд, если чат ещё в статусе pending, отправляет клиенту уведомление."""
    await asyncio.sleep(delay)
    try:
        chat = await get_chat_by_id(support_chat_id)
        if chat and str(chat.get("status")) == "pending":
            await add_chat_message(support_chat_id, SENDER_SPEC, PENDING_REPLY_TEXT)
            await bot.send_message(tg_chat_id, PENDING_REPLY_TEXT)
    except Exception as e:
        logger.debug("pending chat auto-reply failed: %s", e)


async def _client_display_name(uid: str, tg_first_name: str | None = None) -> str:
    """Имя клиента только из нашей БД (поле users.first_name). Telegram-имя/username не используем."""
    try:
        u = await get_user(str(uid)) or {}
    except Exception as e:
        print(e)
        u = {}
    name = (u.get("first_name") or "").strip()
    return name or "Клиент"

def status_ru(status: str | None) -> str:
    """Отображение статуса заявки для клиента на русском."""
    s = (status or "").strip().lower()
    return {
        "pending": "Обрабатывается",
        "active": "В работе",
        "test": "Тест",
        "closed": "Закрыто",
        "declined": "Отклонено",
        "consultation": "Консультация",
    }.get(s, status or "—")

WAIT_TEXT = "Ожидайте мы свяжемcя с вами в ближайшее время"


async def _require_verified_for_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Создание новых обращений доступно только верифицированным клиентам."""
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

    await send_or_edit(
        update,
        "⛔ Обращения доступны только после верификации.\n\n"
        "Подтвердите телефон и укажите ИНН.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
            [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
        ]),
        parse_mode=None,
        context=context,
    )
    return False

# profession: любые int (расширяемо)
PROF_SUPPORT = 1
PROF_ACCOUNTING = 0
PROF_SERVICES = 2

# новый отдел магазина: торговое оборудование
PROF_EQUIPMENT = 3  # ⬅️ новый id профессии для специалиста по оборудованию

# консультации из магазина: код -> (title, profession, back_callback_data)
SHOP_CONSULT_SECTIONS: Dict[str, tuple[str, int, str]] = {
    "shop": ("Магазин", PROF_SERVICES, "buy"),
    "ofd": ("Коды ОФД", PROF_SERVICES, "services_ofd"),
    "service": ("Сервисы", PROF_SERVICES, "services_service"),
    "service_orders": ("Сервисы (по заказу)", PROF_SERVICES, "services_service"),
    "edo": ("ЭДО", PROF_SERVICES, "services_edo"),
    "fn": ("ФН", PROF_SERVICES, "services_fn"),
    "equipment": ("Торговое оборудование", PROF_EQUIPMENT, "shop_equipment"),
    "kkt": ("Кассы (ККТ)", PROF_EQUIPMENT, "shop_kkt"),
}



SUPPORT_TYPES = ["Ошибка", "Настройка", "Обновление", "Приложение", "Иное"]
ACCOUNTING_TYPES = ["Консультация", "Запросы ФНС и др.", "Сдача и подача документации", "📝 Подписание УПД"]

SUPPORT_CATEGORIES = ["1С", "Онлайн Касса", "Касса ПК + ФР.", "Кассовое ПО", "Честный Знак", "ЕГАИС", "ЭДО"]
ACCOUNTING_CATEGORIES = [
    "Ответы на запросы контролирующих органов",
    "Заявления изменения",
    "Отчетность ФНС",
    "Рос потреб. надзор",
    "ЕГАИС",
    "Статистика",
]

# =========================
# Support wizard (по схеме)
# =========================

# ВАЖНО: оставляем callback-ы cc_type / cc_cat как есть,
# поэтому PROF_SUPPORT идёт через многошаговую "категорию".

SUPPORT_TYPES = [
    "🖥 Касса не работает (ошибка)",
    "📡 ОФД (не отвечает)",
    "🔄 Продление сервисов",
    "🧾 1С (Ошибки // Доработки)",
    "📌 Иное (другой вопрос)",
]

SUP_SUB_MENUS = {
    "ofd": [
        "ОФД не отвечает",
        "Исчерпан ресурс хранения ФН (ошибка 235)",
        "Продлить ОФД",
    ],
    "ext": [
        "Продлить сервис / программу / приложение",
        "Продлить 1С",
        "Продлить ОФД",
    ],
    "1c": [
        "Ошибка 1С",
        "Обновить 1С",
        "Доработать 1С",
    ],
}

# (можно оставить старый список, он почти не будет использоваться для PROF_SUPPORT)
SUPPORT_CATEGORIES = SUPPORT_CATEGORIES if "SUPPORT_CATEGORIES" in globals() else []

SUP_KKT_CASES = [
    "Я знаю код ошибки / модель",
    "ПК/1С не видит кассу",
]

SUP_KKT_DEVICE_TYPES = [
    "Встроенный принтер / весы",
    "ФР (внешний)",
    "Смарт-терминал",
    "Не знаю",
]

SUP_KKT_ERR_CAPTURE = [
    "Ввести код ошибки",
    "Отправить фото экрана",
    "Написать текстом, что написано",
]

SUP_KKT_WORKS_NOW = [
    "Вообще не даёт работать",
    "Чеки пробивает, но с ошибками",
    "Не знаю",
]

# ⚠️ список можешь подправить под схему 1-в-1
SUP_KKT_SYMPTOMS = [
    "Не пробивает чек / не печатает",
    "Не открывается / не закрывается смена",
    "Сбилось время / дата",
    "Ошибка ФН / ФД / переполнена память",
    "Нет связи / интернет / ОФД",
    "ПК/1С не видит кассу",
    "Другое",
]

SUP_PROG_CASES = [
    "Помогите, 1С не работает",
    "Я знаю код ошибки",
]


def _sup_reset(context):
    context.user_data.pop("cc_sup_step", None)
    context.user_data.pop("cc_sup_items", None)
    context.user_data.pop("cc_sup_prompt", None)
    context.user_data.pop("cc_sup_back_where", None)
    context.user_data["cc_sup_stack"] = []
    context.user_data["cc_sup_meta"] = []


def _sup_meta_add(context, k: str, v: str):
    meta = context.user_data.setdefault("cc_sup_meta", [])
    meta.append((k, v))


def _sup_meta_text(context) -> str:
    meta = context.user_data.get("cc_sup_meta") or []
    if not meta:
        return ""
    lines = [f"• {k}: {v}" for k, v in meta]
    return "🧾 Данные для специалиста:\n" + "\n".join(lines)


def _sup_set_step(context, *, step: str, items: list[str], prompt: str, back_where: str, push_current: bool = True):
    # сохраняем текущее состояние в стек (для cc_back:sup)
    if push_current:
        cur_step = context.user_data.get("cc_sup_step")
        if cur_step:
            stack = context.user_data.setdefault("cc_sup_stack", [])

            meta = context.user_data.get("cc_sup_meta") or []
            meta_len = len(meta)

            stack.append({
                "step": cur_step,
                "items": context.user_data.get("cc_sup_items") or [],
                "prompt": context.user_data.get("cc_sup_prompt") or "",
                "back_where": context.user_data.get("cc_sup_back_where") or "type",
                "meta_len": meta_len,  # 👈 важно для отката meta
            })

    context.user_data["cc_sup_step"] = step
    context.user_data["cc_sup_items"] = items
    context.user_data["cc_sup_prompt"] = prompt
    context.user_data["cc_sup_back_where"] = back_where



def _kb_sup_items(items: list[str], profession: int, back_where: str):
    # back_where:
    #   - "type" -> cc_back:type:<profession> (назад к выбору раздела)
    #   - "sup"  -> cc_back:sup:<profession>  (назад на предыдущий шаг мастера)
    rows = [[InlineKeyboardButton(t, callback_data=f"cc_cat:{profession}:{i}")]
            for i, t in enumerate(items)]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data=f"cc_back:{back_where}:{profession}")])
    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")])
    return InlineKeyboardMarkup(rows)


def _support_content_prompt(cc, context) -> str:
    # Варианты финального текста перед вводом сообщения (по выбранной ветке)
    rt = (cc.request_type or "").lower()

    if "1с" in rt:
        return (
            "✍️ Опишите проблему в 1С.\n"
            "Желательно указать:\n"
            "• версия платформы 1С (если знаете)\n"
            "• что именно не работает и когда началось\n\n"
            "Если нужно — прикрепите файл/скриншот к этому же сообщению.\n"
            "Отправьте одним сообщением."
        )

    if "касса" in rt or "ошибка" in rt:
        return (
            "✍️ Опишите проблему с кассой.\n"
            "Если у вас есть код ошибки — напишите его.\n"
            "Если проще — приложите фото/скрин.\n\n"
            "Отправьте одним сообщением (можно с файлом)."
        )

    if "програм" in rt or "прилож" in rt:
        return (
            "✍️ Опишите проблему в программе/приложении.\n"
            "Если есть код ошибки — напишите его.\n\n"
            "Если нужно — прикрепите скрин.\n"
            "Отправьте одним сообщением."
        )

    return (
        "✍️ Опишите ваш вопрос максимально подробно.\n\n"
        "Если нужно — прикрепите файл/скриншот к этому же сообщению.\n"
        "Отправьте одним сообщением."
    )


@dataclass
class ClientChatCtx:
    profession: int
    request_type: Optional[str] = None
    category: Optional[str] = None
    promo_code: Optional[str] = None


# ---------------- Keyboards ----------------

def _kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Вернуться в меню", callback_data="cc_menu")]])


def _kb_content_prompt(profession: int) -> InlineKeyboardMarkup:
    """Клавиатура на экране ввода первого сообщения: Написать (WebApp) + Вернуться в меню."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Написать", callback_data=f"cc_open_chat:{profession}")],
        [InlineKeyboardButton("🔙 Вернуться в меню", callback_data="cc_menu")],
    ])


def _kb_my_chats(chats: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for ch in chats:
        prof = int(ch.get("profession") or -1)
        prof_label = (
            "🧾 Бухгалтерия" if prof == PROF_ACCOUNTING
            else "🛠 Поддержка" if prof == PROF_SUPPORT
            else "🧰 Услуги" if prof == PROF_SERVICES
            else "🧰 Оборудование" if prof == PROF_EQUIPMENT
            else f"👤 Проф.{prof}"
        )
        st = ch.get("status")
        badge = "🟢" if st == "active" or st == "consultation" else "🟡" if st == "pending" else "⚪"
        title = ch.get("category") or ch.get("request_type") or f"Чат #{ch.get('id')}"
        rows.append([InlineKeyboardButton(f"{badge} {prof_label}: {title}", callback_data=f"cc_mychat:{ch['id']}")])

    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")])
    return InlineKeyboardMarkup(rows)


def _kb_types(profession: int) -> InlineKeyboardMarkup:
    items = SUPPORT_TYPES if profession == PROF_SUPPORT else ACCOUNTING_TYPES
    rows = [[InlineKeyboardButton(t, callback_data=f"cc_type:{profession}:{i}")] for i, t in enumerate(items)]
    rows.append([InlineKeyboardButton("🔙 Вернуться в меню", callback_data="cc_menu")])
    return InlineKeyboardMarkup(rows)


def _kb_sub(items: list, profession: int, group: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(t, callback_data=f"cc_sub:{profession}:{group}:{i}")] for i, t in enumerate(items)]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"cc_back:type:{profession}")])
    return InlineKeyboardMarkup(rows)


def _kb_categories(profession: int) -> InlineKeyboardMarkup:
    items = SUPPORT_CATEGORIES if profession == PROF_SUPPORT else ACCOUNTING_CATEGORIES
    rows = [[InlineKeyboardButton(t, callback_data=f"cc_cat:{profession}:{i}")] for i, t in enumerate(items)]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data=f"cc_back:type:{profession}")])
    rows.append([InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")])
    return InlineKeyboardMarkup(rows)


def _kb_promo_choice(profession: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, ввести промокод", callback_data=f"cc_promo_yes:{profession}")],
        [InlineKeyboardButton("⏭️ Ввести позже", callback_data=f"cc_promo_no:{profession}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"cc_back:cat:{profession}")],
        [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
    ])


def _kb_content_actions(profession: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить обращение", callback_data=f"cc_send:{profession}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"cc_back:cat:{profession}")],
        [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
    ])



def _kb_after_incoming(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Завершить обращение", callback_data=f"cc_close:{chat_id}")],
        [InlineKeyboardButton("В чат", callback_data=f"cc_mychat:{chat_id}")],
        [InlineKeyboardButton("🔙 Вернуться в меню", callback_data="cc_menu")],
    ])

def _kb_close_pending(chat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура, когда отправлен запрос на завершение и ждём вторую сторону."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Продолжить общение", callback_data=f"cc_close_no:{chat_id}")],
        [InlineKeyboardButton("🔙 Вернуться в меню", callback_data="cc_menu")],
    ])


# ---------------- Helpers ----------------

def _ctx_get(context: ContextTypes.DEFAULT_TYPE) -> ClientChatCtx:
    d = context.user_data.get("client_chat_ctx") or {}
    return ClientChatCtx(
        profession=int(d.get("profession", PROF_SUPPORT)),
        request_type=d.get("request_type"),
        category=d.get("category"),
        promo_code=d.get("promo_code"),
    )


def _ctx_set(context: ContextTypes.DEFAULT_TYPE, cc: ClientChatCtx) -> None:
    context.user_data["client_chat_ctx"] = {
        "profession": int(cc.profession),
        "request_type": cc.request_type,
        "category": cc.category,
        "promo_code": cc.promo_code,
    }


def _draft_init(context: ContextTypes.DEFAULT_TYPE) -> None:
    # draft: {"text": str|None, "files":[{"chat_id":int,"message_id":int}], "created_at":ts}
    if "cc_draft" not in context.user_data:
        context.user_data["cc_draft"] = {"text": None, "files": [], "created_at": int(time.time())}


def _draft_clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("cc_draft", None)

async def _extract_tg_file(m):
    if m.document:
        return m.document.file_id, m.document.file_name, m.document.mime_type
    if m.photo:
        p = m.photo[-1]
        return p.file_id, "photo.jpg", "image/jpeg"
    if m.video:
        return m.video.file_id, "video.mp4", m.video.mime_type
    if m.audio:
        return m.audio.file_id, "audio.mp3", m.audio.mime_type
    if m.voice:
        return m.voice.file_id, "voice.ogg", "audio/ogg"
    return None, None, None


def _is_file_message(m: Message) -> bool:
    return bool(m.document or m.photo or m.video or m.audio or m.voice or m.video_note or m.animation)

async def send_or_replace_notice_for_chat(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    tg_chat_id: int,
    support_chat_id: int,
    text: str,
    reply_markup=None,
    parse_mode=None,
) -> int:
    """
    1 служебное сообщение на 1 support_chat_id в чате пользователя.
    Новое уведомление -> редактирует старое.
    """
    mid = await upsert_notice_tg(
        tg_bot=context.application.bot,
        tg_chat_id=int(tg_chat_id),
        user_id=int(tg_chat_id),
        kind=NoticeKind.CLIENT_CHAT,
        entity_id=int(support_chat_id),
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_web_page_preview=True,
    )
    # Legacy-совместимость на переходном этапе:
    # старый ключ больше не является основной логикой, но поддерживаем
    # его до полного удаления старых мест чтения.
    context.user_data[f"cc_notice_mid:{support_chat_id}"] = int(mid)
    return int(mid)


async def clear_notice_for_chat(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    tg_chat_id: int,
    support_chat_id: int,
) -> None:
    await delete_notice_tg(
        tg_bot=context.application.bot,
        tg_chat_id=int(tg_chat_id),
        user_id=int(tg_chat_id),
        kind=NoticeKind.CLIENT_CHAT,
        entity_id=int(support_chat_id),
    )
    context.user_data.pop(f"cc_notice_mid:{support_chat_id}", None)


async def _clear_other_notice_messages_for_current_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Если callback пришёл из notice — удалить остальные notice этого кластера."""

    q = update.callback_query
    if not q or not q.message:
        return
    try:
        await clear_other_notices_for_user_tg(
            tg_bot=context.application.bot,
            tg_chat_id=int(update.effective_user.id),
            user_id=int(update.effective_user.id),
            cluster=NoticeCluster.CLIENT_CHAT_NOTICES,
            current_message_id=int(q.message.message_id),
        )
    except Exception:
        logger.debug("best-effort clear other client notices failed", exc_info=True)

# =========================================================
# ENTRY: запуск интерфейса (только profession)
# =========================================================
async def client_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE, *, profession: int) -> int:
    if await is_specialist_user(update, context):
        from bot_agent_chat import agent_entry
        return await agent_entry(update, context)

    # Определяем партнёра ДО проверки verified — партнёры обходят её
    uid = str(update.effective_user.id)
    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    user = await get_user(uid) or {}
    identity = await resolve_partner_identity_by_phone(
        internal_user_id=int(internal_uid) if internal_uid else None,
        tg_user_id=int(uid),
        phone=user.get("phone"),
    )
    if identity.get("partner_detected"):
        canonical_uid = int(identity.get("canonical_user_id") or 0)
        if canonical_uid > 0:
            try:
                await ensure_partner_entrypoint_chat(
                    partner_user_id=canonical_uid,
                    tg_user_id=int(uid),
                    phone=identity.get("partner_phone") or user.get("phone"),
                    source="telegram",
                )
            except Exception as _pce:
                logger.warning("TG client_chat_start ensure_partner_entrypoint_chat failed: %s", _pce)
            logger.info("TG client_chat_start: partner detected, redirecting to my chats tg_user_id=%s canonical_user_id=%s", uid, canonical_uid)
            return await cb_my_chats(update, context)

    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    try:
        await ensure_support_chat_schema()
    except Exception as e:
        print(e)
        pass

    cc = ClientChatCtx(profession=int(profession))
    _ctx_set(context, cc)
    _draft_clear(context)

    # сбрасываем мастер техподдержки
    _sup_reset(context)

    title = "Выберите раздел техподдержки:" if cc.profession == PROF_SUPPORT else "Выберите тип обращения:"

    await send_or_edit(
        update,
        title,
        _kb_types(cc.profession),
        parse_mode=None,
    )
    return CC_WAIT_TYPE



# =========================================================
# Шаг 1: Тип обращения
# =========================================================
async def cb_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    q = update.callback_query
    await q.answer()

    # cc_type:<profession>:<idx>
    _, prof_s, idx_s = (q.data or "").split(":")
    profession = int(prof_s)
    idx = int(idx_s)

    cc = _ctx_get(context)
    cc.profession = profession

    # ----- PROF_SUPPORT: мастер по схеме -----
    if profession == PROF_SUPPORT:
        _sup_reset(context)

        # выбранный корневой раздел
        root = SUPPORT_TYPES[idx] if 0 <= idx < len(SUPPORT_TYPES) else None
        # нормализуем
        if root:
            root_plain = root.replace("🧾", "").replace("❗", "").replace("💻", "").replace("📌", "").replace("🖥", "").replace("📡", "").replace("🔄", "").strip()
        else:
            root_plain = "Техподдержка"

        cc.request_type = root_plain
        cc.category = None
        _ctx_set(context, cc)

        _sup_meta_add(context, "Раздел", root_plain)

        # ветвление
        if "офд" in root_plain.lower():
            await q.edit_message_text("Выберите подраздел ОФД:", reply_markup=_kb_sub(SUP_SUB_MENUS["ofd"], profession, "ofd"))
            return CC_WAIT_CATEGORY

        if "продление" in root_plain.lower():
            await q.edit_message_text("Выберите что продлить:", reply_markup=_kb_sub(SUP_SUB_MENUS["ext"], profession, "ext"))
            return CC_WAIT_CATEGORY

        if root_plain.startswith("1С"):
            await q.edit_message_text("Выберите подраздел 1С:", reply_markup=_kb_sub(SUP_SUB_MENUS["1c"], profession, "1c"))
            return CC_WAIT_CATEGORY

        if "касса" in root_plain.lower() or "ошибка" in root_plain.lower():
            cc.category = "Касса не работает"
            _ctx_set(context, cc)
            await q.edit_message_text(_support_content_prompt(cc, context), reply_markup=_kb_content_prompt(cc.profession))
            return CC_WAIT_CONTENT

        # Иное -> сразу к сообщению
        cc.category = root_plain
        _ctx_set(context, cc)
        await q.edit_message_text(_support_content_prompt(cc, context), reply_markup=_kb_content_prompt(cc.profession))
        return CC_WAIT_CONTENT

    # ----- остальные профессии: старое поведение -----
    types = SUPPORT_TYPES if profession == PROF_SUPPORT else ACCOUNTING_TYPES
    cc.request_type = types[idx] if 0 <= idx < len(types) else None
    _ctx_set(context, cc)

    # Подписание УПД — показываем подменю «Все» / «Выборочно»
    if cc.request_type and "УПД" in cc.request_type:
        await q.edit_message_text(
            "Выберите вариант подписания УПД:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Все", callback_data=f"cc_upd_all:{profession}")],
                [InlineKeyboardButton("✏️ Выборочно", callback_data=f"cc_upd_selective:{profession}")],
                [InlineKeyboardButton("🔙 Назад", callback_data=f"cc_back:type:{profession}")],
                [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
            ])
        )
        return CC_WAIT_CATEGORY

    await q.edit_message_text("Укажите категорию обращения:", reply_markup=_kb_categories(cc.profession))
    return CC_WAIT_CATEGORY



# =========================================================
# Шаг 1б: Подменю техподдержки (ОФД / Продление / 1С)
# =========================================================
async def cb_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    # cc_sub:<profession>:<group>:<sub_idx>
    parts = (q.data or "").split(":")
    profession = int(parts[1])
    group = parts[2]
    sub_idx = int(parts[3])

    items = SUP_SUB_MENUS.get(group, [])
    category = items[sub_idx] if 0 <= sub_idx < len(items) else "Иное"

    cc = _ctx_get(context)
    cc.category = category
    _ctx_set(context, cc)

    await q.edit_message_text(_support_content_prompt(cc, context), reply_markup=_kb_content_prompt(profession))
    return CC_WAIT_CONTENT


# =========================================================
# Шаг 2: Категория
# =========================================================
async def cb_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    # cc_cat:<profession>:<idx>
    _, prof_s, idx_s = (q.data or "").split(":")
    profession = int(prof_s)
    idx = int(idx_s)

    cc = _ctx_get(context)
    cc.profession = profession

    # ----- PROF_SUPPORT: многошаговый мастер -----
    if profession == PROF_SUPPORT:
        step = context.user_data.get("cc_sup_step")
        items = context.user_data.get("cc_sup_items") or []
        choice = items[idx] if 0 <= idx < len(items) else None
        if not step or not choice:
            # страховка: вернём в начало
            await q.edit_message_text("Выберите раздел техподдержки:", reply_markup=_kb_types(PROF_SUPPORT))
            return CC_WAIT_TYPE

        # Касса
        if step == "kkt_case":
            _sup_meta_add(context, "Сценарий", choice)

            if "знаю код" in choice.lower():
                _sup_set_step(
                    context,
                    step="kkt_device",
                    items=SUP_KKT_DEVICE_TYPES,
                    prompt="Уточните тип кассы/устройства:",
                    back_where="sup",
                )
                await q.edit_message_text(context.user_data["cc_sup_prompt"],
                                          reply_markup=_kb_sup_items(context.user_data["cc_sup_items"], profession, "sup"))
                return CC_WAIT_CATEGORY

            # "касса не работает" -> симптомы
            _sup_set_step(
                context,
                step="kkt_symptom",
                items=SUP_KKT_SYMPTOMS,
                prompt="Выберите, что именно не работает:",
                back_where="sup",
            )
            await q.edit_message_text(context.user_data["cc_sup_prompt"],
                                      reply_markup=_kb_sup_items(context.user_data["cc_sup_items"], profession, "sup"))
            return CC_WAIT_CATEGORY

        if step == "kkt_symptom":
            _sup_meta_add(context, "Что не работает", choice)
            cc.category = f"Касса: {choice}"
            _ctx_set(context, cc)

            await q.edit_message_text(_support_content_prompt(cc, context), reply_markup=_kb_content_prompt(cc.profession))
            return CC_WAIT_CONTENT

        if step == "kkt_device":
            _sup_meta_add(context, "Тип устройства", choice)
            cc.category = f"Касса: {choice}"
            _ctx_set(context, cc)

            _sup_set_step(
                context,
                step="kkt_err_capture",
                items=SUP_KKT_ERR_CAPTURE,
                prompt="Как вам удобнее передать ошибку?",
                back_where="sup",
            )
            await q.edit_message_text(context.user_data["cc_sup_prompt"],
                                      reply_markup=_kb_sup_items(context.user_data["cc_sup_items"], profession, "sup"))
            return CC_WAIT_CATEGORY

        if step == "kkt_err_capture":
            _sup_meta_add(context, "Как передадите ошибку", choice)

            _sup_set_step(
                context,
                step="kkt_works_now",
                items=SUP_KKT_WORKS_NOW,
                prompt="Касса сейчас работает частично?",
                back_where="sup",
            )
            await q.edit_message_text(context.user_data["cc_sup_prompt"],
                                      reply_markup=_kb_sup_items(context.user_data["cc_sup_items"], profession, "sup"))
            return CC_WAIT_CATEGORY

        if step == "kkt_works_now":
            _sup_meta_add(context, "Сейчас работает", choice)

            await q.edit_message_text(_support_content_prompt(cc, context), reply_markup=_kb_content_prompt(cc.profession))
            return CC_WAIT_CONTENT

        # Программа/приложение
        if step == "prog_case":
            _sup_meta_add(context, "Сценарий", choice)
            cc.category = f"Программа: {choice}"
            _ctx_set(context, cc)

            await q.edit_message_text(_support_content_prompt(cc, context), reply_markup=_kb_content_prompt(cc.profession))
            return CC_WAIT_CONTENT

        # fallback
        await q.edit_message_text(_support_content_prompt(cc, context), reply_markup=_kb_content_prompt(cc.profession))
        return CC_WAIT_CONTENT

    # ----- остальные профессии: старое поведение -----
    cats = SUPPORT_CATEGORIES if profession == PROF_SUPPORT else ACCOUNTING_CATEGORIES
    cc.category = cats[idx] if 0 <= idx < len(cats) else None

    cc.promo_code = None
    _ctx_set(context, cc)

    await q.edit_message_text(
        "✍️ Введите сообщение.\n"
        "Если нужно — прикрепите файл к этому же сообщению.\n\n"
        "Отправьте одним сообщением.",
        reply_markup=_kb_content_prompt(cc.profession),
    )
    return CC_WAIT_CONTENT





# =========================================================
# Шаг 3: Промокод (Да/Нет) + ввод
# =========================================================
async def cb_promo_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    cc = _ctx_get(context)
    cc.promo_code = None
    _ctx_set(context, cc)

    await q.edit_message_text(
        "Введите промокод одним сообщением:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data=f"cc_back:promo_choice:{cc.profession}")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
        ]),
    )
    return CC_WAIT_PROMO_INPUT


async def cb_promo_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    cc = _ctx_get(context)
    cc.promo_code = None
    _ctx_set(context, cc)

    _draft_init(context)
    await q.edit_message_text(
        "Отправьте текст обращения и при необходимости файлы.\n"
        "Можно отправить несколько сообщений/файлов.\n\n"
        "Когда будете готовы — нажмите «Отправить обращение».",
        reply_markup=_kb_content_actions(cc.profession),
    )
    return CC_WAIT_CONTENT


async def on_promo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text:
        await send_temporary_message(update, context, "Промокод пустой. Введите промокод текстом.")
        return CC_WAIT_PROMO_INPUT

    cc = _ctx_get(context)
    cc.promo_code = text
    _ctx_set(context, cc)

    _draft_init(context)
    await update.effective_chat.send_message(
        "Отправьте текст обращения и при необходимости файлы.\n"
        "Можно отправить несколько сообщений/файлов.\n\n"
        "Когда будете готовы — нажмите «Отправить обращение».",
        reply_markup=_kb_content_actions(cc.profession),
    )
    return CC_WAIT_CONTENT


# =========================================================
# Шаг 4: Контент (текст + файлы)
# =========================================================
async def _send_like(bot, to_chat_id: int, m: Message, html_text: str, reply_markup=None) -> Message:
    # Текст
    if m.text and not _is_file_message(m):
        return await bot.send_message(
            chat_id=to_chat_id,
            text=html_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    # Файлы/медиа (пытаемся отправить 1 сообщением с caption)
    if m.document:
        return await bot.send_document(to_chat_id, document=m.document.file_id, caption=html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if m.photo:
        return await bot.send_photo(to_chat_id, photo=m.photo[-1].file_id, caption=html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if m.video:
        return await bot.send_video(to_chat_id, video=m.video.file_id, caption=html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if m.audio:
        return await bot.send_audio(to_chat_id, audio=m.audio.file_id, caption=html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if m.voice:
        return await bot.send_voice(to_chat_id, voice=m.voice.file_id, caption=html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if m.animation:
        return await bot.send_animation(to_chat_id, animation=m.animation.file_id, caption=html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    # video_note не поддерживает caption → редкий кейс: 2 сообщения
    msg = await bot.send_message(to_chat_id, text=html_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    await bot.copy_message(to_chat_id, from_chat_id=m.chat_id, message_id=m.message_id)
    return msg


async def on_content_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    m = update.effective_message
    cc = _ctx_get(context)

    uid_int = int(update.effective_user.id)
    uid = str(uid_int)

    # берём текст из текста или caption
    txt = (m.text or m.caption or "").strip()
    has_file = _is_file_message(m)

    # --- ✅ добавляем шапку мастера техподдержки (meta) ---
    if int(cc.profession) == PROF_SUPPORT:
        hdr = _sup_meta_text(context)
        if hdr:
            txt = f"{hdr}\n\n{txt}" if txt else hdr

    if (not txt or txt.startswith("/")) and not has_file:
        await send_temporary_message(update, context, "Пришлите текст и/или файл одним сообщением.")
        return CC_WAIT_CONTENT

    title = (cc.category or cc.request_type or "Обращение").strip()
    client_name = await _client_display_name(uid, update.effective_user.first_name)

    # если это специалист — уводим в агентский интерфейс
    if (await is_specialist_user(update, context)):
        await update.effective_chat.send_message(
            "🧑‍🔧 Режим навигации: вы специалист.\n"
            "Заявка НЕ создана и другим специалистам НЕ отправлена."
        )
        from bot_agent_chat import agent_entry
        return await agent_entry(update, context)

    profession = int(cc.profession)

    if (not await is_user_verified(uid)):
        await send_or_edit(
            update,
            "⛔ Подача заявки доступна только после верификации профиля.\n\n"
            "Подтвердите телефон и укажите ИНН.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
                [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
            ]),
            parse_mode=None,
            context=context,
        )
        return STATE_IDLE

    # директор нужен для логики распределения (получаем один раз)
    director = await get_director_specialist()

    # 1) УСЛУГИ → только директор
    if profession == PROF_SERVICES:
        if not director:
            await send_temporary_message(update, context, "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            return STATE_IDLE
        specs = [director]

    # 2) Поддержка / Бухгалтерия → специалисты + директор
    else:
        specs = await list_specialists_for_profession(profession) or []

        if director:
            did = int(director.get("id") or 0)
            if did and all(int(s.get("id") or 0) != did for s in specs):
                specs.append(director)

        if not specs:
            await send_temporary_message(update, context, "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            return STATE_IDLE

    # 1) создаём чат
    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    if not internal_uid:
        await send_temporary_message(update, context, "⚠️ Не удалось определить ваш профиль. Попробуйте /start ещё раз.")
        return STATE_IDLE

    chat_id = await create_support_chat_request(
        internal_user_id=internal_uid,
        profession=int(cc.profession),
        category=(cc.category or None),
        request_type=(cc.request_type or None),
        source="telegram",
    )
    if has_file:
        file_id, fname, mime = await _extract_tg_file(m)

        if file_id:
            tg_file = await context.application.bot.get_file(file_id)
            data = await tg_file.download_as_bytearray()

            token = await create_chat_access_token(
                chat_id=int(chat_id),
                role="client",
                actor_id=str(uid),
            )

            url = f"{WEBAPP_URL}/api/chats/{int(chat_id)}/files?token={token}"

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    data={"sender": "0", "text": (txt or "")[:4000]},
                    files={"file": (fname, bytes(data), mime)},
                )
                # если WebApp не принял файл — не падаем, но пишем в историю предупреждение
                if resp.status_code >= 400:
                    await add_chat_message(int(chat_id), SENDER_CLIENT, f"⚠️ Не удалось загрузить файл в WebApp ({resp.status_code}).")


    # 2) история
    # Текст/файл мы стараемся класть в WebApp-чат (через API). На всякий случай —
    # сохраняем текст напрямую только когда файла нет.
    if (not has_file) and txt:
        await add_chat_message(int(chat_id), SENDER_CLIENT, txt)

    # 3) рассылаем всем специалистам (одним сообщением)

    prefix = (
        f"💬 Сообщение от <b>{escape(client_name)}</b> "
        f"по вопросу <b>{escape(title)}</b>"
    )
    full = prefix + (f"\n\n{escape(txt)}" if txt else "")

    for s in specs:
        if not isinstance(s, dict):
            continue

        tg_to = s.get("tg_user_id")
        max_uid = int(s.get("max_user_id") or 0)
        if not tg_to and not max_uid:
            continue

        # директору по НЕ-услугам отправляем только «Открыть чат» (режим наблюдателя)
        s_role = str(s.get("role") or "specialist")
        token_role = "director" if s_role == "director" else "agent"

        if tg_to:
            if s_role == "director" and profession != PROF_SERVICES:
                kb = InlineKeyboardMarkup([
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="ag_back_to_chats")],
                ])
            else:
                kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Принять", callback_data=f"ag_offer_yes:{chat_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"ag_offer_no:{chat_id}"),
                    ],
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                ])
            try:
                sent_mid = await upsert_notice_tg(
                    tg_bot=context.application.bot,
                    tg_chat_id=int(tg_to),
                    user_id=int(tg_to),
                    kind=NoticeKind.SPECIALIST_CHAT,
                    entity_id=int(chat_id),
                    text=full,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )

                if not (s_role == "director" and profession != PROF_SERVICES):
                    await add_chat_offer(int(chat_id), int(s.get("id") or 0), int(tg_to), int(sent_mid))
            except Exception as e:
                print(e)

        # Дублируем уведомление в MAX (если у специалиста привязан max_user_id)
        if max_uid:
            try:
                if s_role == "director" and profession != PROF_SERVICES:
                    max_kb = [
                        [{"type": "callback", "text": "📂 Открыть список", "payload": "ag_refresh"}],
                    ]
                else:
                    max_kb = [
                        [
                            {"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{chat_id}"},
                            {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{chat_id}"},
                        ],
                    ]
                await notify_specialist_max(max_uid, full, keyboard_buttons=max_kb)
            except Exception as e:
                print(f"MAX notify error for spec {s.get('id')}: {e}")

    # 4) ответ клиенту
    await update.effective_chat.send_message(
        "✅ Обращение отправлено. Ожидайте ответа.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 Мои обращения", callback_data="cc_my_chats")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
        ]),
    )

    # Best-effort: удаляем исходное сообщение клиента с текстом обращения.
    try:
        await context.application.bot.delete_message(
            chat_id=int(update.effective_chat.id),
            message_id=int(m.message_id),
        )
    except Exception:
        logger.debug("TG delete user request message failed", exc_info=True)

    # ✅ сбрасываем мастер техподдержки, чтобы не залипали данные
    _sup_reset(context)

    asyncio.create_task(_schedule_pending_chat_reply(
        context.application.bot,
        int(update.effective_chat.id),
        int(chat_id),
    ))

    _ctx_reset(context)
    return STATE_IDLE


async def create_client_chat_text_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
) -> int:
    """Создать клиентское обращение с готовым текстом без ожидания ввода сообщения."""
    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    cc = _ctx_get(context)
    uid_int = int(update.effective_user.id)
    uid = str(uid_int)
    txt = (text or "").strip()
    if not txt:
        await send_temporary_message(update, context, "Не удалось сформировать текст обращения.")
        return STATE_IDLE

    title = (cc.category or cc.request_type or "Обращение").strip()
    client_name = await _client_display_name(uid, update.effective_user.first_name)

    if await is_specialist_user(update, context):
        await update.effective_chat.send_message(
            "🧑‍🔧 Режим навигации: вы специалист.\n"
            "Заявка НЕ создана и другим специалистам НЕ отправлена."
        )
        from bot_agent_chat import agent_entry
        return await agent_entry(update, context)

    profession = int(cc.profession)

    if not await is_user_verified(uid):
        await send_or_edit(
            update,
            "⛔ Подача заявки доступна только после верификации профиля.\n\n"
            "Подтвердите телефон и укажите ИНН.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔐 Закончить верификацию", callback_data="register")],
                [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")],
            ]),
            parse_mode=None,
            context=context,
        )
        return STATE_IDLE

    director = await get_director_specialist()
    if profession == PROF_SERVICES:
        if not director:
            await send_temporary_message(update, context, "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            return STATE_IDLE
        specs = [director]
    else:
        specs = await list_specialists_for_profession(profession) or []
        if director:
            did = int(director.get("id") or 0)
            if did and all(int(s.get("id") or 0) != did for s in specs):
                specs.append(director)
        if not specs:
            await send_temporary_message(update, context, "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            return STATE_IDLE

    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    if not internal_uid:
        await send_temporary_message(update, context, "⚠️ Не удалось определить ваш профиль. Попробуйте /start ещё раз.")
        return STATE_IDLE

    chat_id = await create_support_chat_request(
        internal_user_id=internal_uid,
        profession=profession,
        category=(cc.category or None),
        request_type=(cc.request_type or None),
        source="telegram",
    )
    await add_chat_message(int(chat_id), SENDER_CLIENT, txt)

    prefix = (
        f"💬 Сообщение от <b>{escape(client_name)}</b> "
        f"по вопросу <b>{escape(title)}</b>"
    )
    full = prefix + f"\n\n{escape(txt)}"

    for s in specs:
        if not isinstance(s, dict):
            continue

        tg_to = s.get("tg_user_id")
        max_uid = int(s.get("max_user_id") or 0)
        if not tg_to and not max_uid:
            continue

        s_role = str(s.get("role") or "specialist")
        token_role = "director" if s_role == "director" else "agent"
        if tg_to:
            if s_role == "director" and profession != PROF_SERVICES:
                kb = InlineKeyboardMarkup([
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="ag_back_to_chats")],
                ])
            else:
                kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Принять", callback_data=f"ag_offer_yes:{chat_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"ag_offer_no:{chat_id}"),
                    ],
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                ])
            try:
                sent_mid = await upsert_notice_tg(
                    tg_bot=context.application.bot,
                    tg_chat_id=int(tg_to),
                    user_id=int(tg_to),
                    kind=NoticeKind.SPECIALIST_CHAT,
                    entity_id=int(chat_id),
                    text=full,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                if not (s_role == "director" and profession != PROF_SERVICES):
                    await add_chat_offer(int(chat_id), int(s.get("id") or 0), int(tg_to), int(sent_mid))
            except Exception as e:
                print(e)

        if max_uid:
            try:
                if s_role == "director" and profession != PROF_SERVICES:
                    max_kb = [[{"type": "callback", "text": "📂 Открыть список", "payload": "ag_refresh"}]]
                else:
                    max_kb = [[
                        {"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{chat_id}"},
                        {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{chat_id}"},
                    ]]
                await notify_specialist_max(max_uid, full, keyboard_buttons=max_kb)
            except Exception as e:
                print(f"MAX notify error for spec {s.get('id')}: {e}")

    await update.effective_chat.send_message(
        "✅ Обращение отправлено. Ожидайте ответа.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 Мои обращения", callback_data="cc_my_chats")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
        ]),
    )

    asyncio.create_task(_schedule_pending_chat_reply(
        context.application.bot,
        int(update.effective_chat.id),
        int(chat_id),
    ))

    _sup_reset(context)
    _ctx_reset(context)
    return STATE_IDLE


from db import (
    create_support_chat_request,
    list_specialists_for_profession,
    get_open_chat_for_client,
    force_assign_chat,
    get_least_busy_specialist_for_profession,
)



def _draft_reset(context: ContextTypes.DEFAULT_TYPE) -> None:
    _draft_clear(context)

def _ctx_reset(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("client_chat_ctx", None)


async def _redirect_specialist_to_agent_if_needed(update, context) -> int | None:
    """Если пользователь является специалистом — редиректим в агентское меню и возвращаем код состояния.
    Возвращает None, если пользователь не специалист (продолжаем обычный клиентский flow)."""
    if await is_specialist_user(update, context):
        from bot_agent_chat import agent_entry
        return await agent_entry(update, context)
    return None


# =========================================================
# «Написать» на экране ввода сообщения — открываем чат в WebApp
# =========================================================
async def cb_open_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback: cc_open_chat:<profession>
    Создаёт обращение заранее и возвращает кнопку WebApp,
    не требуя вводить текст прямо в боте.
    """
    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    q = update.callback_query
    await q.answer()

    uid = str(update.effective_user.id)

    redirect = await _redirect_specialist_to_agent_if_needed(update, context)
    if redirect is not None:
        return redirect

    profession = int((q.data or "").split(":")[1])

    if not await is_user_verified(uid):
        await q.edit_message_text(
            "⛔ Подача заявки доступна только после верификации профиля.",
            reply_markup=_kb_menu(),
        )
        return STATE_IDLE

    cc = _ctx_get(context)
    cc.profession = profession
    _ctx_set(context, cc)

    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    if not internal_uid:
        await q.edit_message_text("⚠️ Не удалось определить ваш профиль.", reply_markup=_kb_menu())
        return STATE_IDLE

    chat_id = await create_support_chat_request(
        internal_user_id=internal_uid,
        profession=profession,
        category=cc.category,
        request_type=cc.request_type,
        source="telegram",
    )
    await add_chat_message(int(chat_id), SENDER_CLIENT, "Клиент открыл обращение")

    # Уведомляем специалистов
    client_name = await _client_display_name(uid, update.effective_user.first_name)
    title = (cc.category or cc.request_type or "Обращение").strip()
    full = (
        f"💬 Сообщение от <b>{escape(client_name)}</b> "
        f"по вопросу <b>{escape(title)}</b>"
    )
    director = await get_director_specialist()
    specs = await list_specialists_for_profession(profession) or []
    if director:
        did = int(director.get("id") or 0)
        if did and all(int(s.get("id") or 0) != did for s in specs):
            specs.append(director)

    for s in specs:
        if not isinstance(s, dict):
            continue
        tg_to = s.get("tg_user_id")
        max_uid_s = int(s.get("max_user_id") or 0)
        s_role = str(s.get("role") or "specialist")
        token_role = "director" if s_role == "director" else "agent"
        if tg_to:
            if s_role == "director" and profession != PROF_SERVICES:
                kb_s = InlineKeyboardMarkup([
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="ag_back_to_chats")],
                ])
            else:
                kb_s = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Принять", callback_data=f"ag_offer_yes:{chat_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"ag_offer_no:{chat_id}"),
                    ],
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                ])
            try:
                sent_mid = await upsert_notice_tg(
                    tg_bot=context.application.bot,
                    tg_chat_id=int(tg_to),
                    user_id=int(tg_to),
                    kind=NoticeKind.SPECIALIST_CHAT,
                    entity_id=int(chat_id),
                    text=full,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_s,
                    disable_web_page_preview=True,
                )
                if not (s_role == "director" and profession != PROF_SERVICES):
                    await add_chat_offer(int(chat_id), int(s.get("id") or 0), int(tg_to), int(sent_mid))
            except Exception as e:
                logger.warning("cb_open_chat notify error: %s", e)
        if max_uid_s:
            try:
                max_kb = [[
                    {"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{chat_id}"},
                    {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{chat_id}"},
                ]]
                await notify_specialist_max(max_uid_s, full, keyboard_buttons=max_kb)
            except Exception as e:
                logger.warning("cb_open_chat MAX notify error: %s", e)

    # Сохраняем chat_id для возможного ответа прямо в боте
    context.user_data["cc_reply_chat_id"] = chat_id
    _sup_reset(context)

    kb = InlineKeyboardMarkup([
        [await chat_webapp_button(chat_id=chat_id, role="client", actor_id=update.effective_user.id, text="💬 Открыть чат")],
        [InlineKeyboardButton("✍️ Написать здесь", callback_data=f"cc_reply:{chat_id}")],
        [InlineKeyboardButton("📂 Мои обращения", callback_data="cc_my_chats")],
        [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
    ])
    await q.edit_message_text(
        "✅ Обращение создано.\n\n"
        "Нажмите «Открыть чат», чтобы написать в мини-приложении.\n"
        "Или нажмите «Написать здесь» — ответ придёт прямо в бот.",
        reply_markup=kb,
    )
    return CC_WAIT_REPLY


# =========================================================
# Подписание УПД — «Все» (создаём задачу без ввода текста)
# =========================================================
async def cb_upd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    q = update.callback_query
    await q.answer()

    redirect = await _redirect_specialist_to_agent_if_needed(update, context)
    if redirect is not None:
        return redirect

    profession = int((q.data or "").split(":")[1])
    uid = str(update.effective_user.id)

    if not await is_user_verified(uid):
        await send_or_edit(
            update,
            "⛔ Подача заявки доступна только после верификации профиля.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]),
            parse_mode=None, context=context,
        )
        return STATE_IDLE

    cc = _ctx_get(context)
    cc.profession = profession
    cc.category = "Все"
    _ctx_set(context, cc)

    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    if not internal_uid:
        await send_or_edit(update, "⚠️ Не удалось определить ваш профиль.", _kb_menu(), parse_mode=None, context=context)
        return STATE_IDLE

    chat_id = await create_support_chat_request(
        internal_user_id=internal_uid,
        profession=int(profession),
        category="Все",
        request_type="Подписание УПД",
        source="telegram",
    )
    await add_chat_message(int(chat_id), SENDER_CLIENT, "Подписание УПД — все документы")

    client_name = await _client_display_name(uid, update.effective_user.first_name)
    title = "Подписание УПД — Все"
    full = f"💬 Сообщение от <b>{escape(client_name)}</b> по вопросу <b>{escape(title)}</b>"
    director = await get_director_specialist()
    specs = await list_specialists_for_profession(profession) or []
    if director:
        did = int(director.get("id") or 0)
        if did and all(int(s.get("id") or 0) != did for s in specs):
            specs.append(director)

    for s in specs:
        if not isinstance(s, dict):
            continue
        tg_to = s.get("tg_user_id")
        max_uid_s = int(s.get("max_user_id") or 0)
        s_role = str(s.get("role") or "specialist")
        token_role = "director" if s_role == "director" else "agent"
        if tg_to:
            if s_role == "director" and profession != PROF_SERVICES:
                kb = InlineKeyboardMarkup([
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="ag_back_to_chats")],
                ])
            else:
                kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Принять", callback_data=f"ag_offer_yes:{chat_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"ag_offer_no:{chat_id}"),
                    ],
                    [await chat_webapp_button(chat_id=chat_id, role=token_role, actor_id=int(tg_to), text="💬 Ответить")],
                ])
            try:
                sent_mid = await upsert_notice_tg(
                    tg_bot=context.application.bot,
                    tg_chat_id=int(tg_to),
                    user_id=int(tg_to),
                    kind=NoticeKind.SPECIALIST_CHAT,
                    entity_id=int(chat_id),
                    text=full,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                if not (s_role == "director" and profession != PROF_SERVICES):
                    await add_chat_offer(int(chat_id), int(s.get("id") or 0), int(tg_to), int(sent_mid))
            except Exception as e:
                logger.warning("cb_upd_all notify error: %s", e)
        if max_uid_s:
            try:
                max_kb = [
                    [{"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{chat_id}"},
                     {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{chat_id}"}],
                ]
                await notify_specialist_max(max_uid_s, full, keyboard_buttons=max_kb)
            except Exception as e:
                logger.warning("cb_upd_all MAX notify error: %s", e)

    await q.edit_message_text(
        "✅ Обращение «Подписание УПД — Все» отправлено. Ожидайте ответа.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 Мои обращения", callback_data="cc_my_chats")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
        ]),
    )
    _sup_reset(context)
    return STATE_IDLE


# =========================================================
# Подписание УПД — «Выборочно» (клиент пишет сам)
# =========================================================
async def cb_upd_selective(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    q = update.callback_query
    await q.answer()

    profession = int((q.data or "").split(":")[1])
    cc = _ctx_get(context)
    cc.profession = profession
    cc.request_type = "Подписание УПД"
    cc.category = "Выборочно"
    _ctx_set(context, cc)

    await q.edit_message_text(
        "✍️ Напишите, какие именно документы нужно подписать.\n\n"
        "Если нужно — прикрепите файл/скриншот.\n"
        "Отправьте одним сообщением.",
        reply_markup=_kb_content_prompt(cc.profession),
    )
    return CC_WAIT_CONTENT


# =========================================================
# Назад по шагам
# =========================================================
async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    # cc_back:<where>:<profession>
    _, where, prof_s = (q.data or "").split(":")
    profession = int(prof_s)

    cc = _ctx_get(context)
    cc.profession = profession
    _ctx_set(context, cc)

    # ✅ Назад внутри мастера техподдержки
    if where == "sup":
        stack = context.user_data.get("cc_sup_stack") or []
        if stack:
            prev = stack.pop()
            context.user_data["cc_sup_stack"] = stack

            # восстановим step/кнопки
            context.user_data["cc_sup_step"] = prev["step"]
            context.user_data["cc_sup_items"] = prev["items"]
            context.user_data["cc_sup_prompt"] = prev["prompt"]
            context.user_data["cc_sup_back_where"] = prev.get("back_where", "type")

            # 👇 откатываем meta (работает вместе с обновлённым _sup_set_step)
            meta = context.user_data.get("cc_sup_meta") or []
            meta_len = int(prev.get("meta_len", len(meta)))
            context.user_data["cc_sup_meta"] = meta[:meta_len]

            await q.edit_message_text(
                prev["prompt"],
                reply_markup=_kb_sup_items(prev["items"], profession, prev.get("back_where", "type")),
            )
            return CC_WAIT_CATEGORY

        # если стека нет — вернём к выбору раздела техподдержки
        await q.edit_message_text("Выберите раздел техподдержки:", reply_markup=_kb_types(PROF_SUPPORT))
        return CC_WAIT_TYPE

    # обычные переходы назад (как было)
    if where == "type":
        text = "Выберите раздел техподдержки:" if cc.profession == PROF_SUPPORT else "Выберите тип обращения:"
        await q.edit_message_text(text, reply_markup=_kb_types(cc.profession))
        return CC_WAIT_TYPE

    if where == "cat":
        await q.edit_message_text("Укажите категорию обращения:", reply_markup=_kb_categories(cc.profession))
        return CC_WAIT_CATEGORY

    # старые переходы (promo/promo_choice) — считаем как "назад в категории"
    if where in ("promo", "promo_choice"):
        _draft_clear(context)
        await q.edit_message_text("Укажите категорию обращения:", reply_markup=_kb_categories(cc.profession))
        return CC_WAIT_CATEGORY

    # fallback
    text = "Выберите раздел техподдержки:" if cc.profession == PROF_SUPPORT else "Выберите тип обращения:"
    await q.edit_message_text(text, reply_markup=_kb_types(cc.profession))
    return CC_WAIT_TYPE



async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
        await _clear_other_notice_messages_for_current_callback(update, context)
    _draft_clear(context)

    clear_chat_reply_flags(context, update.effective_user.id)

    # важно: вышли из чата
    context.user_data.pop("cc_reply_chat_id", None)

    await clear_user_notices_tg(
        tg_bot=context.application.bot,
        tg_chat_id=int(update.effective_user.id),
        user_id=int(update.effective_user.id),
    )
    # Legacy cleanup на переходном этапе
    for k in list(context.user_data.keys()):
        if str(k).startswith("cc_notice_mid:"):
            context.user_data.pop(k, None)

    return await show_main_menu(update, context)



# =========================================================
# 2.3) Сообщение от специалиста → клиенту (3 кнопки)
# =========================================================

async def cb_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    chat_id = int((q.data or "").split(":", 1)[1])
    context.user_data["cc_reply_chat_id"] = chat_id
    await send_temporary_message(update, context, "Введите ваш ответ:")
    return CC_WAIT_REPLY


async def on_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Клиент отвечает в существующий чат из Telegram.
    Важно: ничего не пересылаем специалисту файлом/медиа через бота.
    Всё отправляем в WebApp-чат через API (там хранится история и приходят уведомления).
    """
    chat_id = int(context.user_data.get("cc_reply_chat_id") or 0)
    if not chat_id:
        return await show_main_menu(update, context, "Не нашёл активный чат. Начните обращение заново.")

    m: Message = update.effective_message
    has_file = _is_file_message(m)
    text = (m.text or m.caption or "").strip()

    # если это команда и нет файла — игнорируем
    if (not text or text.startswith("/")) and not has_file:
        await send_temporary_message(update, context, "Отправьте текст и/или файл одним сообщением.")
        return CC_WAIT_REPLY

    chat = await get_chat_by_id(chat_id)
    if not chat:
        return await show_main_menu(update, context, "Чат не найден.")

    internal_uid = await resolve_internal_user_id(tg_user_id=str(update.effective_user.id))
    if not internal_uid or int(chat.get("client_user_id") or 0) != int(internal_uid):
        return await show_main_menu(update, context, "Это не ваш чат.")

    if str(chat.get("status")) == "closed":
        return await show_main_menu(update, context, "Чат уже закрыт. Создайте новое обращение.")

    # если специалист ещё не назначен — в WebApp всё равно пишем (как правило, будет запрещено правилами pending),
    # но клиенту сообщим понятнее
    if not chat.get("specialist_id") and str(chat.get("status")) == "pending":
        await send_temporary_message(update, context, "Заявка обрабатывается. Пожалуйста, подождите.")
        return CC_WAIT_REPLY

    token = await create_chat_access_token(
        chat_id=int(chat_id),
        role="client",
        actor_id=str(update.effective_user.id),
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if not has_file:
                url = f"{WEBAPP_URL}/api/chats/{int(chat_id)}/messages?token={token}"
                resp = await client.post(url, json={"sender": 0, "text": text})
                resp.raise_for_status()
            else:
                file_id, fname, mime = await _extract_tg_file(m)
                if not file_id:
                    raise RuntimeError("Не удалось извлечь файл из сообщения.")
                tg_file = await context.application.bot.get_file(file_id)
                data = await tg_file.download_as_bytearray()

                url = f"{WEBAPP_URL}/api/chats/{int(chat_id)}/files?token={token}"
                resp = await client.post(
                    url,
                    data={"sender": "0", "text": (text or "")[:4000]},
                    files={"file": (fname, bytes(data), mime)},
                )
                resp.raise_for_status()

        await send_temporary_message(
            update,
            context,
            "✅ Сообщение отправлено.",
            reply_markup=_kb_after_incoming(chat_id),
        )
    except Exception as e:
        print(e)
        await send_temporary_message(
            update,
            context,
            "⚠️ Не удалось отправить сообщение в чат. Попробуйте ещё раз.",
            reply_markup=_kb_after_incoming(chat_id),
        )

    return CC_WAIT_REPLY


# =========================================================
# 2.5) Завершение обращения специалистом
# =========================================================
# =========================================================
async def deliver_chat_closed(
        *,
        context: ContextTypes.DEFAULT_TYPE,
        client_user_id: str,
        profession: int,
        category: str,
) -> None:
    # chat = await get_active_chat_for_client(client_user_id, profession)
    # if not chat:
        # return

    # chat_id = int(chat["id"])
    # await close_chat(chat_id)

    await context.application.bot.send_message(
        chat_id=int(client_user_id),
        text=(
            f"✅ Ваше обращение <b>{category}</b> завершено.\n\n"
            "При необходимости можете оформить новое обращение."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_menu(),
    )



# =========================================================
# Консультация из магазина (без мастера заявок)
# =========================================================
async def cb_shop_consult(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_verified_for_requests(update, context):
        return STATE_IDLE

    """
    Кнопка «Консультация» внутри магазина.
    Отличие от техподдержки: без мастера категорий — сразу открываем чат.
    Специалист видит, из какого раздела пришёл клиент (category = 'Магазин: ...').
    """
    q = update.callback_query
    if q:
        await q.answer()

    redirect = await _redirect_specialist_to_agent_if_needed(update, context)
    if redirect is not None:
        return redirect

    uid = str(update.effective_user.id)
    code = (q.data or "").split(":", 1)[1] if (q and ":" in (q.data or "")) else "shop"

    title, profession, back_cb = SHOP_CONSULT_SECTIONS.get(code, SHOP_CONSULT_SECTIONS["shop"])
    category = f"Магазин: {title}"
    request_type = "Консультация"

    # 1) выбираем ответственного
    spec: Dict[str, Any] | None = None

    if int(profession) == int(PROF_EQUIPMENT):
        # оборудование — отдельный спец (самый свободный)
        spec = await get_least_busy_specialist_for_profession(int(PROF_EQUIPMENT))
        if not spec:
            # fallback: директор
            spec = await get_director_specialist()
    else:
        # все остальные разделы — директор
        spec = await get_director_specialist()

    if not spec:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)],
            [InlineKeyboardButton("🏪 Разделы магазина", callback_data="buy")],
        ])
        await send_or_edit(update, "⚠️ Сейчас нет доступных специалистов. Попробуйте позже.", kb, context=context)
        return STATE_IDLE

    specialist_id = int(spec.get("id") or 0)
    specialist_tg = spec.get("tg_user_id")

    # 2) не плодим дубликаты: ищем открытый чат по profession+category+type
    chat = await get_open_chat_for_client(
        client_user_id=uid,
        profession=int(profession),
        request_type=request_type,
        category=category,
    )

    chat_id: int
    created_now = False

    if chat:
        chat_id = int(chat["id"])
        # если вдруг остался pending без назначенного — назначим сразу
        if str(chat.get("status")) == "pending" and not chat.get("specialist_id"):
            await force_assign_chat(chat_id, specialist_id, True)
    else:
        internal_uid = await resolve_internal_user_id(tg_user_id=uid)
        if not internal_uid:
            await send_temporary_message(update, context, "⚠️ Не удалось определить ваш профиль. Попробуйте /start ещё раз.")
            return STATE_IDLE
        chat_id = await create_support_chat_request(
            internal_user_id=internal_uid,
            profession=int(profession),
            category=category,
            request_type=request_type,
            source="telegram",
        )
        await force_assign_chat(chat_id, specialist_id, True)
        created_now = True

        # системная запись (чтобы в истории было понятно откуда пришёл)
        try:
            await add_chat_message(int(chat_id), SENDER_CLIENT, f"💬 Консультация из магазина: {title}")
        except Exception:
            pass

    # 3) пингуем специалиста (только если чат создан сейчас, чтобы не спамить)
    if created_now and specialist_tg:
        try:
            client_name = await _client_display_name(uid, update.effective_user.first_name)
            msg = (
                "💬 <b>Консультация из магазина</b>\n"
                f"Раздел: <b>{escape(title)}</b>\n"
                f"Клиент: <b>{escape(client_name)}</b>\n\n"
                "Нажмите «Ответить», чтобы ответить."
            )
            kb_spec = InlineKeyboardMarkup([
                [await chat_webapp_button(chat_id=int(chat_id), role="agent", actor_id=int(specialist_tg), text="💬 Ответить")],
                [InlineKeyboardButton("📂 Заявки", callback_data="ag_refresh")],
            ])
            await upsert_notice_tg(
                tg_bot=context.application.bot,
                tg_chat_id=int(specialist_tg),
                user_id=int(specialist_tg),
                kind=NoticeKind.SPECIALIST_CHAT,
                entity_id=int(chat_id),
                text=msg,
                parse_mode=ParseMode.HTML,
                reply_markup=kb_spec,
            )
        except Exception:
            pass

    # 4) экран клиенту
    kb_client = InlineKeyboardMarkup([
        [await chat_webapp_button(chat_id=int(chat_id), role="client", actor_id=uid, text="💬 Ответить")],
        [InlineKeyboardButton("⬅️ Вернуться в раздел", callback_data=back_cb)],
        [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton("🏪 Разделы магазина", callback_data="buy")],
    ])

    text = (
        "💬 <b>Консультация</b>\n"
        f"Раздел: <b>{escape(title)}</b>\n\n"
        "Нажмите «Ответить», чтобы написать специалисту."
    )

    # если экран был с фото (карточка ККТ) — редактировать текст нельзя, поэтому удаляем и шлём новый
    try:
        if q and q.message and getattr(q.message, "photo", None):
            chat_id_tg = int(q.message.chat_id)
            try:
                await q.message.delete()
            except Exception:
                pass
            await context.application.bot.send_message(
                chat_id=chat_id_tg,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb_client,
            )
            return STATE_IDLE
    except Exception:
        pass

    await send_or_edit(update, text, kb_client, parse_mode=ParseMode.HTML, context=context)
    return STATE_IDLE


async def cb_my_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer()
        await _clear_other_notice_messages_for_current_callback(update, context)

    uid = str(update.effective_user.id)
    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    user = await get_user(uid) or {}
    identity = await resolve_partner_identity_by_phone(
        internal_user_id=int(internal_uid) if internal_uid else None,
        tg_user_id=int(uid),
        phone=user.get("phone"),
    )
    canonical_uid = int(identity.get("canonical_user_id") or 0) or (int(internal_uid) if internal_uid else 0)
    partner_chat_id = None
    if canonical_uid:
        try:
            partner_chat_id = await ensure_partner_entrypoint_chat(
                partner_user_id=canonical_uid,
                tg_user_id=int(uid),
                phone=identity.get("partner_phone") or user.get("phone"),
                source="telegram",
            )
        except Exception as _pce:
            logger.warning("TG ensure_partner_entrypoint_chat failed: %s", _pce)
    logger.info(
        "TG partner_my_chats: tg_user_id=%s current_internal_user_id=%s current_phone=%s partner_detected=%s partner_specialist_id=%s canonical_user_id=%s ensured_chat_id=%s",
        uid,
        internal_uid,
        user.get("phone"),
        identity.get("partner_detected"),
        identity.get("partner_specialist_id"),
        canonical_uid or None,
        partner_chat_id,
    )

    if not canonical_uid:
        await send_or_edit(update, "Не найден внутренний профиль пользователя. Обновите телефон в профиле и повторите.", _kb_menu(), parse_mode=None)
        return STATE_IDLE

    chats = await list_chats_for_client(internal_user_id=canonical_uid)
    logger.info("TG partner_my_chats: tg_user_id=%s chats_count=%s", uid, len(chats))
    chats = sorted(
        chats,
        key=lambda ch: 0 if str(ch.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE else 1,
    )

    if not chats:
        await send_or_edit(update, "У вас нет активных обращений.", _kb_menu(), parse_mode=None)
        return STATE_IDLE

    await send_or_edit(update, "💬 Ваши обращения:\nВыберите чат:", _kb_my_chats(chats), parse_mode=None)
    return STATE_IDLE


async def cb_mychat_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await _clear_other_notice_messages_for_current_callback(update, context)

    # cc_mychat:<chat_id>
    logger.info("TG cb_mychat_open: callback_data=%s tg_user_id=%s", q.data if q else None, update.effective_user.id if update.effective_user else None)
    chat_id = int((q.data or "").split(":")[1])
    chat = await get_chat_by_id(chat_id)

    uid = str(update.effective_user.id)
    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    identity = await resolve_partner_identity_by_phone(internal_user_id=int(internal_uid) if internal_uid else None, tg_user_id=int(uid))
    access_uid = int(identity.get("canonical_user_id") or 0) if identity.get("partner_detected") else int(internal_uid or 0)
    if not chat or access_uid <= 0 or int(chat.get("client_user_id") or 0) != access_uid:
        await q.edit_message_text("Чат не найден.", reply_markup=_kb_menu())
        return STATE_IDLE
    await clear_notice_for_chat(
        context,
        tg_chat_id=int(update.effective_user.id),
        support_chat_id=chat_id,
    )

    title = chat.get("client_name") or f"Клиент {chat.get('client_user_id')}"
    text = (
        f"💬 <b>Чат</b>\n"
        f"Тип: <b>{chat.get('request_type') or '—'}</b>\n"
        f"Категория: <b>{chat.get('category') or '—'}</b>\n"
        f"Статус: <b>{status_ru(chat.get('status'))}</b>\n\n"
        "✍️ Напишите ответ прямо здесь (кнопка «Написать здесь»)\n"
        "или откройте переписку в мини-приложении (кнопка «Ответить»)."
    )
    
    is_partner_director_chat = str(chat.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE
    if chat.get('status')!="consultation" and not is_partner_director_chat:
        kb = InlineKeyboardMarkup([
            [await chat_webapp_button(
                chat_id=chat_id,
                role="client",
                actor_id=update.effective_user.id, text="💬 Ответить")],
            [InlineKeyboardButton("✍️ Написать здесь", callback_data=f"cc_reply:{chat_id}")],
            [InlineKeyboardButton("✅ Завершить обращение", callback_data=f"cc_close:{chat_id}")],
            [InlineKeyboardButton("📂 Мои обращения", callback_data="cc_my_chats")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [await chat_webapp_button(
                chat_id=chat_id,
                role="client",
                actor_id=update.effective_user.id, text="💬 Ответить")],
            [InlineKeyboardButton("✍️ Написать здесь", callback_data=f"cc_reply:{chat_id}")],
            [InlineKeyboardButton("📂 Мои обращения", callback_data="cc_my_chats")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cc_menu")],
        ])
    
    await send_or_edit(
        update,
        text,
        kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        context=context,
    )
    return STATE_IDLE



async def cb_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await _clear_other_notice_messages_for_current_callback(update, context)

    # cc_close:<chat_id>
    chat_id = int((q.data or "").split(":")[1])

    chat = await get_chat_by_id(chat_id)
    uid = str(update.effective_user.id)

    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    identity = await resolve_partner_identity_by_phone(internal_user_id=int(internal_uid) if internal_uid else None, tg_user_id=int(uid))
    access_uid = int(identity.get("canonical_user_id") or 0) if identity.get("partner_detected") else int(internal_uid or 0)
    if not chat or access_uid <= 0 or int(chat.get("client_user_id") or 0) != access_uid:
        await q.edit_message_text("Чат не найден или он не ваш.", reply_markup=_kb_menu())
        return STATE_IDLE
    if str(chat.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE:
        await q.edit_message_text("⛔ Постоянный чат с директором нельзя закрыть.", reply_markup=_kb_menu())
        return STATE_IDLE

    if str(chat.get("status")) == "closed":
        await q.edit_message_text("Этот чат уже закрыт.", reply_markup=_kb_menu())
        return STATE_IDLE

    # === новая логика: просим закрыть, а закрываем только при согласии обеих сторон ===
    res = await request_chat_close(chat_id, by="client")
    if not res.get("ok"):
        await q.edit_message_text("Не удалось обработать запрос. Попробуйте ещё раз.", reply_markup=_kb_menu())
        return STATE_IDLE

    # Если специалист уже согласился — чат закроется сразу
    if res.get("just_closed"):
        await add_chat_message(chat_id, SENDER_CLIENT, "✅ Обращение завершено по согласию обеих сторон")

        # уведомим специалиста на обеих платформах (если назначен)
        spec_id = chat.get("specialist_id")
        if spec_id:
            try:
                spec = await get_specialist_by_id(int(spec_id))
                if spec:
                    close_text = f"✅ Обращение закрыто по согласию обеих сторон (чат #{chat_id})."
                    await notify_specialist_all_platforms(
                        spec, close_text,
                        tg_reply_markup={"inline_keyboard": [
                            [{"text": "⬅️ К списку чатов", "callback_data": "ag_back_to_chats"}],
                        ]},
                        max_keyboard_buttons=[
                            [{"type": "callback", "text": "⬅️ К списку чатов", "payload": "ag_refresh"}],
                        ],
                    )
            except Exception as e:
                logger.error("Close notify failed for chat %s: %s", chat_id, e)

        await q.edit_message_text(
            "✅ Обращение завершено (подтверждено обеими сторонами).\n"
            "Если нужна помощь снова — создайте новое обращение через меню.",
            reply_markup=_kb_menu(),
        )
        return STATE_IDLE

    # Иначе ждём специалиста
    await add_chat_message(chat_id, SENDER_CLIENT, "🔒 Клиент запросил завершение обращения (ожидаем)")

    # уведомим специалиста на обеих платформах с кнопками подтвердить/отклонить
    spec_id = chat.get("specialist_id")
    if spec_id:
        close_req_text = f"🔒 Клиент запросил завершение обращения (чат #{chat_id}).\nПодтвердить закрытие?"
        try:
            spec = await get_specialist_by_id(int(spec_id))
            if spec:
                await notify_specialist_all_platforms(
                    spec, close_req_text,
                    tg_reply_markup={"inline_keyboard": [
                        [
                            {"text": "✅ Подтвердить", "callback_data": f"ag_close_yes:{chat_id}"},
                            {"text": "❌ Не закрывать", "callback_data": f"ag_close_cancel:{chat_id}"},
                        ],
                        [{"text": "💬 Ответить", "callback_data": f"ag_chat:{chat_id}"}],
                    ]},
                    max_keyboard_buttons=[
                        [
                            {"type": "callback", "text": "✅ Подтвердить", "payload": f"ag_close_yes:{chat_id}"},
                            {"type": "callback", "text": "❌ Не закрывать", "payload": f"ag_close_cancel:{chat_id}"},
                        ],
                        [{"type": "callback", "text": "💬 Ответить", "payload": f"ag_chat:{chat_id}"}],
                    ],
                )
        except Exception as e:
            logger.error("Close request notify failed for chat %s: %s", chat_id, e)

    await q.edit_message_text(
        "🔒 Запрос на завершение отправлен.\n"
        "Чат закроется только после подтверждения завершения.",
        reply_markup=_kb_close_pending(chat_id),
    )
    return STATE_IDLE


async def cb_close_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Клиент отклоняет/отзывает закрытие (сбрасываем флаги и продолжаем чат)."""
    q = update.callback_query
    await q.answer()
    await _clear_other_notice_messages_for_current_callback(update, context)

    # cc_close_no:<chat_id>
    chat_id = int((q.data or "").split(":")[1])
    chat = await get_chat_by_id(chat_id)
    uid = str(update.effective_user.id)

    internal_uid = await resolve_internal_user_id(tg_user_id=uid)
    identity = await resolve_partner_identity_by_phone(internal_user_id=int(internal_uid) if internal_uid else None, tg_user_id=int(uid))
    access_uid = int(identity.get("canonical_user_id") or 0) if identity.get("partner_detected") else int(internal_uid or 0)
    if not chat or access_uid <= 0 or int(chat.get("client_user_id") or 0) != access_uid:
        await q.edit_message_text("Чат не найден или он не ваш.", reply_markup=_kb_menu())
        return STATE_IDLE
    if str(chat.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE:
        await q.edit_message_text("⛔ Постоянный чат с директором нельзя закрыть.", reply_markup=_kb_menu())
        return STATE_IDLE

    if str(chat.get("status")) == "closed":
        await q.edit_message_text("Этот чат уже закрыт.", reply_markup=_kb_menu())
        return STATE_IDLE

    await reset_chat_close_votes(chat_id)
    await add_chat_message(chat_id, SENDER_CLIENT, "↩️ Клиент отменил завершение (продолжаем общение)")

    # уведомим специа
