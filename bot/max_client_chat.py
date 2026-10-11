"""
max_client_chat.py — Клиентский чат для MAX-бота.

Перенос логики из bot_client_chat.py:
- Создание обращения (бухгалтерия / техподдержка / услуги)
- Список «Мои обращения»
- Просмотр отдельного чата
- Ответ в существующий чат
- Закрытие обращения (двустороннее)

Адаптация для MAX:
- WebApp через кнопку open_app (мини-приложение MAX)

- Состояния через user_data["_state"]
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

from max_api import MaxMessage, MaxCallback, InlineKeyboard, InlineButton
from max_bot_main import MaxBotContext
from max_registration import _send_or_edit, _set_state, _show_main_menu, _resolve_db_id, _resolve_chat_id, ST_IDLE, send_temporary_message_max
from notice_center import (
    NoticeCluster,
    NoticeKind,
    clear_other_notices_for_user_max,
    delete_notice_max,
)

import sys
from pathlib import Path
import httpx

_bot_dir = Path(__file__).resolve().parent
_project_dir = _bot_dir.parent
sys.path.insert(0, str(_project_dir))

from db import (
    get_user_by_internal_id,
    is_user_verified_by_internal_id,
    get_chat_by_id,
    list_chats_for_client,
    add_chat_message,
    request_chat_close,
    reset_chat_close_votes,
    get_specialist_by_id,
    get_director_specialist,
    create_support_chat_request,
    list_specialists_for_profession,
    ensure_support_chat_schema,
    add_chat_offer,
    create_chat_access_token,
    PARTNER_DIRECTOR_REQUEST_TYPE,
    ensure_partner_entrypoint_chat,
    resolve_partner_identity_by_phone,
)

from cross_notify import notify_specialist_all_platforms

logger = logging.getLogger(__name__)

import html as _html

def _escape_html(s: str) -> str:
    return _html.escape(str(s)) if s else ""

from webapp_settings import WEBAPP_URL, MAX_BOT_NAME

# ── Состояния ─────────────────────────────────────────────────────────
ST_CC_WAIT_TYPE = "cc_wait_type"
ST_CC_WAIT_CATEGORY = "cc_wait_category"
ST_CC_WAIT_CONTENT = "cc_wait_content"
ST_CC_WAIT_REPLY = "cc_wait_reply"

# ── Константы ─────────────────────────────────────────────────────────
SENDER_CLIENT = 0
SENDER_SPEC = 1

PENDING_REPLY_DELAY = 120  # секунд до отправки автоматического сообщения клиенту
PENDING_REPLY_TEXT = "👋 Ваше обращение принято! Специалист скоро с вами свяжется."


async def _schedule_pending_chat_reply_max(
    api,
    max_chat_id: str,
    support_chat_id: int,
    delay: int = PENDING_REPLY_DELAY,
) -> None:
    """Через delay секунд, если чат ещё pending, отправляет клиенту уведомление в MAX."""
    await asyncio.sleep(delay)
    try:
        chat = await get_chat_by_id(support_chat_id)
        if chat and str(chat.get("status")) == "pending":
            await add_chat_message(support_chat_id, SENDER_SPEC, PENDING_REPLY_TEXT)
            await api.send_message(max_chat_id, PENDING_REPLY_TEXT)
    except Exception as e:
        logger.debug("MAX pending chat auto-reply failed: %s", e)


PROF_ACCOUNTING = 0
PROF_SUPPORT = 1
PROF_SERVICES = 2
PROF_EQUIPMENT = 3

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

ACCOUNTING_TYPES = [
    "Консультация",
    "Запросы ФНС и др.",
    "Сдача и подача документации",
    "📝 Подписание УПД",
]

ACCOUNTING_CATEGORIES = [
    "Ответы на запросы контролирующих органов",
    "Заявления изменения",
    "Отчетность ФНС",
    "Рос потреб. надзор",
    "ЕГАИС",
    "Статистика",
]

# ── Подкатегории мастера техподдержки ─────────────────────────────────
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


def _extract_max_file(msg: MaxMessage) -> Optional[Dict[str, Any]]:
    """
    Извлечь первый файл/фото из MAX-сообщения.
    Возвращает dict {"url": ..., "filename": ..., "type": "image"|"file"} или None.
    """
    for att in (msg.attachments or []):
        att_type = att.get("type", "")
        payload = att.get("payload") or {}
        url = payload.get("url")
        if not url:
            continue
        if att_type == "image":
            return {"url": url, "filename": payload.get("filename") or "photo.jpg", "type": "image",
                    "mime": "image/jpeg"}
        if att_type in ("file", "video", "audio"):
            fname = payload.get("filename") or payload.get("name") or "file"
            mime = payload.get("mime_type") or "application/octet-stream"
            return {"url": url, "filename": fname, "type": att_type, "mime": mime}
    return None


async def _upload_file_to_webapp(
    api, support_chat_id: int, token: str,
    file_url: str, filename: str, mime: str,
    sender: int = SENDER_CLIENT, text: str = "",
) -> bool:
    """Скачать файл из MAX и загрузить в WebApp чат API."""
    try:
        file_data = await api.download_file(file_url)
        if not file_data:
            logger.error("_upload_file_to_webapp: empty file from %s", file_url)
            return False

        url = f"{WEBAPP_URL}/api/chats/{int(support_chat_id)}/files?token={token}"
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            resp = await client.post(
                url,
                data={"sender": str(sender), "text": (text or "")[:4000]},
                files={"file": (filename, file_data, mime)},
            )
            if resp.status_code >= 400:
                logger.error("_upload_file_to_webapp: HTTP %s", resp.status_code)
                return False
        return True
    except Exception as e:
        logger.error("_upload_file_to_webapp error: %s", e)
        return False


@dataclass
class ClientChatCtx:
    profession: int
    request_type: Optional[str] = None
    category: Optional[str] = None


def _ctx_get(ctx: MaxBotContext) -> ClientChatCtx:
    d = ctx.user_data.get("client_chat_ctx") or {}
    return ClientChatCtx(
        profession=int(d.get("profession", PROF_SUPPORT)),
        request_type=d.get("request_type"),
        category=d.get("category"),
    )


def _ctx_set(ctx: MaxBotContext, cc: ClientChatCtx) -> None:
    ctx.user_data["client_chat_ctx"] = {
        "profession": int(cc.profession),
        "request_type": cc.request_type,
        "category": cc.category,
    }


def _ctx_reset(ctx: MaxBotContext) -> None:
    ctx.user_data.pop("client_chat_ctx", None)


async def _clear_other_client_notices_for_callback(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Если callback пришёл из notice — удалить остальные client-notices пользователя."""
    if not cb.user or not cb.message:
        return
    try:
        await clear_other_notices_for_user_max(
            max_api=ctx.api,
            max_user_id=int(cb.user.user_id),
            cluster=NoticeCluster.CLIENT_CHAT_NOTICES,
            current_message_id=cb.message.message_id,
        )
    except Exception:
        logger.debug("best-effort clear_other_client_notices_for_callback failed", exc_info=True)


# ── Мастер техподдержки: стек/метаданные ─────────────────────────────

def _sup_reset(ctx: MaxBotContext) -> None:
    """Сбросить состояние мастера техподдержки."""
    ctx.user_data.pop("cc_sup_step", None)
    ctx.user_data.pop("cc_sup_items", None)
    ctx.user_data.pop("cc_sup_prompt", None)
    ctx.user_data.pop("cc_sup_back_where", None)
    ctx.user_data["cc_sup_stack"] = []
    ctx.user_data["cc_sup_meta"] = []


def _sup_meta_add(ctx: MaxBotContext, k: str, v: str) -> None:
    meta = ctx.user_data.setdefault("cc_sup_meta", [])
    meta.append((k, v))


def _sup_set_step(ctx: MaxBotContext, *, step: str, items: list,
                  prompt: str, back_where: str, push_current: bool = True) -> None:
    """Перейти к следующему шагу мастера, сохраняя текущий в стек."""
    if push_current:
        cur_step = ctx.user_data.get("cc_sup_step")
        if cur_step:
            stack = ctx.user_data.setdefault("cc_sup_stack", [])
            meta = ctx.user_data.get("cc_sup_meta") or []
            stack.append({
                "step": cur_step,
                "items": ctx.user_data.get("cc_sup_items") or [],
                "prompt": ctx.user_data.get("cc_sup_prompt") or "",
                "back_where": ctx.user_data.get("cc_sup_back_where") or "type",
                "meta_len": len(meta),
            })

    ctx.user_data["cc_sup_step"] = step
    ctx.user_data["cc_sup_items"] = items
    ctx.user_data["cc_sup_prompt"] = prompt
    ctx.user_data["cc_sup_back_where"] = back_where


def _support_content_prompt(cc: ClientChatCtx, ctx: MaxBotContext) -> str:
    """Финальный prompt перед вводом сообщения (зависит от ветки)."""
    rt = (cc.request_type or "").lower()

    if "1с" in rt:
        return (
            "✍️ Опишите проблему в 1С.\n"
            "Желательно указать:\n"
            "• версия платформы 1С (если знаете)\n"
            "• что именно не работает и когда началось\n\n"
            "Если нужно — прикрепите файл/скриншот.\n"
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
        "Если нужно — прикрепите файл/скриншот.\n"
        "Отправьте одним сообщением."
    )


def _kb_sup_items(items: list, profession: int, back_where: str) -> InlineKeyboard:
    """Клавиатура с вариантами шага мастера + кнопка назад."""
    rows = [[InlineButton(t, callback_data=f"cc_cat:{profession}:{i}")]
            for i, t in enumerate(items)]
    rows.append([InlineButton("🔙 Назад", callback_data=f"cc_back:{back_where}:{profession}")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])
    return InlineKeyboard(rows=rows)


def _kb_categories(profession: int) -> InlineKeyboard:
    """Клавиатура категорий (для бухгалтерии)."""
    items = ACCOUNTING_CATEGORIES
    rows = [[InlineButton(t, callback_data=f"cc_cat:{profession}:{i}")]
            for i, t in enumerate(items)]
    rows.append([InlineButton("🔙 Назад", callback_data=f"cc_back:type:{profession}")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])
    return InlineKeyboard(rows=rows)


def _kb_content_back(profession: int) -> InlineKeyboard:
    """Клавиатура при вводе текста обращения."""
    return InlineKeyboard(rows=[
        [InlineButton("✍️ Написать", callback_data=f"cc_open_chat:{profession}")],
        [InlineButton("🔙 Назад", callback_data=f"cc_back:type:{profession}")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])


def status_ru(status: str | None) -> str:
    s = (status or "").strip().lower()
    return {
        "pending": "Обрабатывается",
        "active": "В работе",
        "test": "Тест",
        "closed": "Закрыто",
        "declined": "Отклонено",
        "consultation": "Консультация",
    }.get(s, status or "—")


async def _client_display_name(db_id: int) -> str:
    try:
        u = await get_user_by_internal_id(db_id) or {}
    except Exception:
        u = {}
    return (u.get("first_name") or "").strip() or "Клиент"


async def _chat_webapp_button(
    support_chat_id: int,
    role: str,
    actor_id: str,
    text: str = "💬 Ответить",
) -> InlineButton:
    """Создаёт кнопку open_app для открытия WebApp-чата в MAX."""
    token = await create_chat_access_token(
        chat_id=support_chat_id,
        role=role,
        actor_id=str(actor_id),
    )
    if MAX_BOT_NAME:
        # Правильный MAX deep-link: токен передаётся через Bridge (start_param)
        url = f"https://max.ru/{MAX_BOT_NAME}?startapp={token}"
    else:
        # Фоллбэк: прямой URL, токен читается из query-param
        url = f"{WEBAPP_URL}/max_entry?payload={token}"
    return InlineButton(text, url=url)


async def _require_verified_for_requests(
    ctx: MaxBotContext,
    chat_id: str,
    *,
    message_id: str | None = None,
) -> bool:
    """Создание и просмотр обращений доступны только верифицированным клиентам."""
    db_id = int(ctx.user_data.get("_db_id") or 0)
    verified = bool(ctx.user_data.get("_verified"))

    if db_id and not verified:
        try:
            verified = bool(await is_user_verified_by_internal_id(db_id))
        except Exception:
            verified = False
        ctx.user_data["_verified"] = verified

    if verified:
        return True

    kb = InlineKeyboard(rows=[
        [InlineButton("🔐 Закончить верификацию", callback_data="register")],
        [InlineButton("👤 Мой профиль", callback_data="profile")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(
        ctx,
        chat_id,
        "⛔ Обращения доступны только после верификации.\n\n"
        "Подтвердите телефон и укажите ИНН.",
        keyboard=kb,
        message_id=message_id,
    )
    return False


# ══════════════════════════════════════════════════════════════════════
# Вход — выбор типа обращения
# ══════════════════════════════════════════════════════════════════════

async def handle_client_chat_start(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """
    Callback: client_chat_start:<profession>
    Точка входа — бухгалтерия (0), техподдержка (1).
    """
    if not cb.user:
        return

    max_uid = str(cb.user.user_id)
    await _resolve_db_id(ctx, max_uid)

    payload = cb.payload or ""
    profession = int(payload.split(":", 1)[1]) if ":" in payload else PROF_SUPPORT
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    # Определяем партнёра ДО проверки verified — партнёры обходят её
    db_id = int(ctx.user_data.get("_db_id") or 0)
    u = await get_user_by_internal_id(db_id) if db_id else {}
    identity = await resolve_partner_identity_by_phone(
        internal_user_id=db_id if db_id else None,
        max_user_id=int(max_uid),
        phone=(u or {}).get("phone"),
    )
    if identity.get("partner_detected"):
        canonical_uid = int(identity.get("canonical_user_id") or 0)
        if canonical_uid > 0:
            try:
                await ensure_partner_entrypoint_chat(
                    partner_user_id=canonical_uid,
                    max_user_id=int(max_uid),
                    phone=identity.get("partner_phone") or (u or {}).get("phone"),
                    source="max",
                )
            except Exception as _pce:
                logger.warning("MAX client_chat_start ensure_partner_entrypoint_chat failed: %s", _pce)
            logger.info("MAX client_chat_start: partner detected, redirecting to my chats max_user_id=%s canonical_user_id=%s", max_uid, canonical_uid)
            await handle_my_chats(ctx, cb)
            return

    if not await _require_verified_for_requests(ctx, chat_id, message_id=mid):
        return

    try:
        await ensure_support_chat_schema()
    except Exception:
        pass

    cc = ClientChatCtx(profession=profession)
    _ctx_set(ctx, cc)
    _sup_reset(ctx)

    items = SUPPORT_TYPES if profession == PROF_SUPPORT else ACCOUNTING_TYPES
    title = "Выберите раздел техподдержки:" if profession == PROF_SUPPORT else "Выберите тип обращения:"

    rows = []
    for i, t in enumerate(items):
        rows.append([InlineButton(t, callback_data=f"cc_type:{profession}:{i}")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    await _send_or_edit(ctx, chat_id, title, keyboard=InlineKeyboard(rows=rows), message_id=mid)
    _set_state(ctx, ST_CC_WAIT_TYPE)


# ══════════════════════════════════════════════════════════════════════
# Шаг 1: выбор типа → сразу к вводу текста
# ══════════════════════════════════════════════════════════════════════

async def handle_cc_type(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_type:<profession>:<idx>
    Для техподдержки — запускает многошаговый мастер.
    Для бухгалтерии — показывает категории.
    """
    if not cb.user:
        return

    payload = cb.payload or ""
    parts = payload.split(":")
    profession = int(parts[1]) if len(parts) > 1 else PROF_SUPPORT
    idx = int(parts[2]) if len(parts) > 2 else 0

    items = SUPPORT_TYPES if profession == PROF_SUPPORT else ACCOUNTING_TYPES
    chosen = items[idx] if 0 <= idx < len(items) else "Иное"

    cc = _ctx_get(ctx)
    cc.profession = profession
    cc.request_type = chosen
    _ctx_set(ctx, cc)

    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    # ── PROF_SUPPORT: многошаговый мастер ──
    if profession == PROF_SUPPORT:
        _sup_reset(ctx)
        root_plain = chosen.replace("🧾", "").replace("❗", "").replace("💻", "").replace("📌", "").replace("🖥", "").replace("📡", "").replace("🔄", "").strip()
        cc.request_type = root_plain
        cc.category = None
        _ctx_set(ctx, cc)
        _sup_meta_add(ctx, "Раздел", root_plain)

        if "офд" in root_plain.lower():
            rows = [[InlineButton(t, callback_data=f"cc_sub:{profession}:ofd:{i}")]
                    for i, t in enumerate(SUP_SUB_MENUS["ofd"])]
            rows.append([InlineButton("⬅️ Назад", callback_data=f"cc_back:type:{profession}")])
            await _send_or_edit(ctx, chat_id, "Выберите подраздел ОФД:",
                                keyboard=InlineKeyboard(rows=rows), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CATEGORY)
            return

        if "продление" in root_plain.lower():
            rows = [[InlineButton(t, callback_data=f"cc_sub:{profession}:ext:{i}")]
                    for i, t in enumerate(SUP_SUB_MENUS["ext"])]
            rows.append([InlineButton("⬅️ Назад", callback_data=f"cc_back:type:{profession}")])
            await _send_or_edit(ctx, chat_id, "Выберите что продлить:",
                                keyboard=InlineKeyboard(rows=rows), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CATEGORY)
            return

        if root_plain.startswith("1С"):
            rows = [[InlineButton(t, callback_data=f"cc_sub:{profession}:1c:{i}")]
                    for i, t in enumerate(SUP_SUB_MENUS["1c"])]
            rows.append([InlineButton("⬅️ Назад", callback_data=f"cc_back:type:{profession}")])
            await _send_or_edit(ctx, chat_id, "Выберите подраздел 1С:",
                                keyboard=InlineKeyboard(rows=rows), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CATEGORY)
            return

        if "касса" in root_plain.lower() or "ошибка" in root_plain.lower():
            cc.category = "Касса не работает"
            _ctx_set(ctx, cc)
            await _send_or_edit(ctx, chat_id, _support_content_prompt(cc, ctx),
                                keyboard=_kb_content_back(profession), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CONTENT)
            return

        # Иное → сразу к сообщению
        cc.category = root_plain
        _ctx_set(ctx, cc)
        await _send_or_edit(ctx, chat_id, _support_content_prompt(cc, ctx),
                            keyboard=_kb_content_back(profession), message_id=mid)
        _set_state(ctx, ST_CC_WAIT_CONTENT)
        return

    # ── Бухгалтерия: для УПД — подменю «Все» / «Выборочно» ──
    if "УПД" in (cc.request_type or ""):
        cc.category = None
        _ctx_set(ctx, cc)
        rows = [
            [InlineButton("📋 Все", callback_data=f"cc_upd_all:{profession}")],
            [InlineButton("✏️ Выборочно", callback_data=f"cc_upd_selective:{profession}")],
            [InlineButton("🔙 Назад", callback_data=f"cc_back:type:{profession}")],
            [InlineButton("🔙 В меню", callback_data="back_to_main")],
        ]
        await _send_or_edit(ctx, chat_id, "Выберите вариант подписания УПД:",
                            keyboard=InlineKeyboard(rows=rows), message_id=mid)
        _set_state(ctx, ST_CC_WAIT_CATEGORY)
        return

    # ── Бухгалтерия: показываем подкатегории ──
    cc.category = None
    _ctx_set(ctx, cc)
    await _send_or_edit(ctx, chat_id, "Укажите категорию обращения:",
                        keyboard=_kb_categories(profession), message_id=mid)
    _set_state(ctx, ST_CC_WAIT_CATEGORY)


async def handle_cc_sub(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_sub:<profession>:<group>:<sub_idx>
    Подменю тех. поддержки: ОФД / Продление / 1С.
    """
    if not cb.user:
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    parts = (cb.payload or "").split(":")
    profession = int(parts[1]) if len(parts) > 1 else 1
    group = parts[2] if len(parts) > 2 else ""
    sub_idx = int(parts[3]) if len(parts) > 3 else 0

    items = SUP_SUB_MENUS.get(group, [])
    category = items[sub_idx] if 0 <= sub_idx < len(items) else "Иное"

    cc = _ctx_get(ctx)
    cc.category = category
    _ctx_set(ctx, cc)

    await _send_or_edit(ctx, chat_id, _support_content_prompt(cc, ctx),
                        keyboard=_kb_content_back(profession), message_id=mid)
    _set_state(ctx, ST_CC_WAIT_CONTENT)


async def handle_cc_category(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_cat:<profession>:<idx>
    Обработка шагов мастера техподдержки и категорий бухгалтерии.
    """
    if not cb.user:
        return

    payload = cb.payload or ""
    parts = payload.split(":")
    profession = int(parts[1]) if len(parts) > 1 else PROF_SUPPORT
    idx = int(parts[2]) if len(parts) > 2 else 0

    cc = _ctx_get(ctx)
    cc.profession = profession

    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    # ── PROF_SUPPORT: многошаговый мастер ──
    if profession == PROF_SUPPORT:
        step = ctx.user_data.get("cc_sup_step")
        items = ctx.user_data.get("cc_sup_items") or []
        choice = items[idx] if 0 <= idx < len(items) else None

        if not step or not choice:
            # страховка: вернём в начало
            rows = [[InlineButton(t, callback_data=f"cc_type:{PROF_SUPPORT}:{i}")]
                    for i, t in enumerate(SUPPORT_TYPES)]
            rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])
            await _send_or_edit(ctx, chat_id, "Выберите раздел техподдержки:",
                                keyboard=InlineKeyboard(rows=rows), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_TYPE)
            return

        # ── Касса ──
        if step == "kkt_case":
            _sup_meta_add(ctx, "Сценарий", choice)
            if "знаю код" in choice.lower():
                _sup_set_step(ctx, step="kkt_device", items=SUP_KKT_DEVICE_TYPES,
                              prompt="Уточните тип кассы/устройства:", back_where="sup")
            else:
                _sup_set_step(ctx, step="kkt_symptom", items=SUP_KKT_SYMPTOMS,
                              prompt="Выберите, что именно не работает:", back_where="sup")
            await _send_or_edit(ctx, chat_id, ctx.user_data["cc_sup_prompt"],
                                keyboard=_kb_sup_items(ctx.user_data["cc_sup_items"], profession, "sup"),
                                message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CATEGORY)
            return

        if step == "kkt_symptom":
            _sup_meta_add(ctx, "Что не работает", choice)
            cc.category = f"Касса: {choice}"
            _ctx_set(ctx, cc)
            await _send_or_edit(ctx, chat_id, _support_content_prompt(cc, ctx),
                                keyboard=_kb_content_back(profession), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CONTENT)
            return

        if step == "kkt_device":
            _sup_meta_add(ctx, "Тип устройства", choice)
            cc.category = f"Касса: {choice}"
            _ctx_set(ctx, cc)
            _sup_set_step(ctx, step="kkt_err_capture", items=SUP_KKT_ERR_CAPTURE,
                          prompt="Как вам удобнее передать ошибку?", back_where="sup")
            await _send_or_edit(ctx, chat_id, ctx.user_data["cc_sup_prompt"],
                                keyboard=_kb_sup_items(ctx.user_data["cc_sup_items"], profession, "sup"),
                                message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CATEGORY)
            return

        if step == "kkt_err_capture":
            _sup_meta_add(ctx, "Как передадите ошибку", choice)
            _sup_set_step(ctx, step="kkt_works_now", items=SUP_KKT_WORKS_NOW,
                          prompt="Касса сейчас работает частично?", back_where="sup")
            await _send_or_edit(ctx, chat_id, ctx.user_data["cc_sup_prompt"],
                                keyboard=_kb_sup_items(ctx.user_data["cc_sup_items"], profession, "sup"),
                                message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CATEGORY)
            return

        if step == "kkt_works_now":
            _sup_meta_add(ctx, "Сейчас работает", choice)
            await _send_or_edit(ctx, chat_id, _support_content_prompt(cc, ctx),
                                keyboard=_kb_content_back(profession), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CONTENT)
            return

        # ── Программа ──
        if step == "prog_case":
            _sup_meta_add(ctx, "Сценарий", choice)
            cc.category = f"Программа: {choice}"
            _ctx_set(ctx, cc)
            await _send_or_edit(ctx, chat_id, _support_content_prompt(cc, ctx),
                                keyboard=_kb_content_back(profession), message_id=mid)
            _set_state(ctx, ST_CC_WAIT_CONTENT)
            return

        # fallback
        await _send_or_edit(ctx, chat_id, _support_content_prompt(cc, ctx),
                            keyboard=_kb_content_back(profession), message_id=mid)
        _set_state(ctx, ST_CC_WAIT_CONTENT)
        return

    # ── Бухгалтерия: выбрали категорию ──
    cats = ACCOUNTING_CATEGORIES
    cc.category = cats[idx] if 0 <= idx < len(cats) else None
    _ctx_set(ctx, cc)

    await _send_or_edit(
        ctx, chat_id,
        "✍️ Введите сообщение.\n"
        "Если нужно — прикрепите файл к этому же сообщению.\n\n"
        "Отправьте одним сообщением.",
        keyboard=_kb_content_back(profession), message_id=mid,
    )
    _set_state(ctx, ST_CC_WAIT_CONTENT)


async def handle_cc_back(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_back:<where>:<profession>
    Навигация назад внутри мастера и между шагами.
    """
    if not cb.user:
        return

    payload = cb.payload or ""
    parts = payload.split(":")
    where = parts[1] if len(parts) > 1 else "type"
    profession = int(parts[2]) if len(parts) > 2 else PROF_SUPPORT

    cc = _ctx_get(ctx)
    cc.profession = profession
    _ctx_set(ctx, cc)

    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    # Назад внутри мастера техподдержки (разворачиваем стек)
    if where == "sup":
        stack = ctx.user_data.get("cc_sup_stack") or []
        if stack:
            prev = stack.pop()
            ctx.user_data["cc_sup_stack"] = stack
            ctx.user_data["cc_sup_step"] = prev["step"]
            ctx.user_data["cc_sup_items"] = prev["items"]
            ctx.user_data["cc_sup_prompt"] = prev["prompt"]
            ctx.user_data["cc_sup_back_where"] = prev.get("back_where", "type")

            # откатываем meta
            meta = ctx.user_data.get("cc_sup_meta") or []
            meta_len = int(prev.get("meta_len", len(meta)))
            ctx.user_data["cc_sup_meta"] = meta[:meta_len]

            await _send_or_edit(
                ctx, chat_id, prev["prompt"],
                keyboard=_kb_sup_items(prev["items"], profession, prev.get("back_where", "type")),
                message_id=mid,
            )
            _set_state(ctx, ST_CC_WAIT_CATEGORY)
            return

        # стек пуст — вернёмся к выбору раздела
        where = "type"

    # Назад к выбору типа обращения
    if where == "type":
        items = SUPPORT_TYPES if profession == PROF_SUPPORT else ACCOUNTING_TYPES
        title = "Выберите раздел техподдержки:" if profession == PROF_SUPPORT else "Выберите тип обращения:"
        rows = [[InlineButton(t, callback_data=f"cc_type:{profession}:{i}")] for i, t in enumerate(items)]
        rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])
        await _send_or_edit(ctx, chat_id, title, keyboard=InlineKeyboard(rows=rows), message_id=mid)
        _set_state(ctx, ST_CC_WAIT_TYPE)
        return

    # Назад к категориям (бухгалтерия)
    if where == "cat":
        await _send_or_edit(ctx, chat_id, "Укажите категорию обращения:",
                            keyboard=_kb_categories(profession), message_id=mid)
        _set_state(ctx, ST_CC_WAIT_CATEGORY)
        return

    # fallback
    items = SUPPORT_TYPES if profession == PROF_SUPPORT else ACCOUNTING_TYPES
    title = "Выберите раздел техподдержки:" if profession == PROF_SUPPORT else "Выберите тип обращения:"
    rows = [[InlineButton(t, callback_data=f"cc_type:{profession}:{i}")] for i, t in enumerate(items)]
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])
    await _send_or_edit(ctx, chat_id, title, keyboard=InlineKeyboard(rows=rows), message_id=mid)
    _set_state(ctx, ST_CC_WAIT_TYPE)


# ══════════════════════════════════════════════════════════════════════
# Шаг 2: ввод текста обращения → создание чата → рассылка специалистам
# ══════════════════════════════════════════════════════════════════════

async def _on_content_message(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Обработка текста/файла обращения."""
    text = (msg.text or "").strip()
    file_info = _extract_max_file(msg)

    if not await _require_verified_for_requests(ctx, msg.chat_id):
        _set_state(ctx, ST_IDLE)
        return True

    if not text and not file_info:
        await send_temporary_message_max(ctx, msg.chat_id, "Пришлите текст обращения одним сообщением.")
        return True

    if text.startswith("/") and not file_info:
        await send_temporary_message_max(ctx, msg.chat_id, "Пришлите текст обращения одним сообщением.")
        return True

    # Prepend wizard metadata (multi-step support wizard context)
    meta = ctx.user_data.get("cc_sup_meta") or []
    if meta:
        meta_lines = [f"{k}: {v}" for k, v in meta]
        text = "\n".join(meta_lines) + "\n\n" + text

    max_uid = str(msg.sender.user_id) if msg.sender else ""
    db_id = await _resolve_db_id(ctx, max_uid) if max_uid else ctx.user_data.get("_db_id", 0)
    if not db_id:
        await send_temporary_message_max(ctx, msg.chat_id, "⚠️ Не удалось определить пользователя. Попробуйте /start.")
        _set_state(ctx, ST_IDLE)
        return True
    cc = _ctx_get(ctx)
    profession = int(cc.profession)
    title = (cc.category or cc.request_type or "Обращение").strip()
    client_name = await _client_display_name(db_id)

    # Получаем специалистов
    director = await get_director_specialist()

    if profession == PROF_SERVICES:
        if not director:
            await ctx.api.send_message(msg.chat_id,
                                       "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            _set_state(ctx, ST_IDLE)
            return True
        specs = [director]
    else:
        specs = await list_specialists_for_profession(profession) or []
        if director:
            did = int(director.get("id") or 0)
            if did and all(int(s.get("id") or 0) != did for s in specs):
                specs.append(director)
        if not specs:
            await ctx.api.send_message(msg.chat_id,
                                       "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            _set_state(ctx, ST_IDLE)
            return True

    # Создаём чат в БД по каноническому internal users.id
    support_chat_id = await create_support_chat_request(
        internal_user_id=db_id,
        profession=profession,
        category=cc.category,
        request_type=cc.request_type,
        ensure_user_exists=False,
        source="max",
    )

    # Если есть файл — загружаем через WebApp API
    if file_info:
        token = await create_chat_access_token(
            chat_id=int(support_chat_id),
            role="client",
            actor_id=str(db_id),
        )
        ok = await _upload_file_to_webapp(
            ctx.api, int(support_chat_id), token,
            file_url=file_info["url"],
            filename=file_info["filename"],
            mime=file_info.get("mime", "application/octet-stream"),
            sender=SENDER_CLIENT,
            text=text,
        )
        if not ok:
            await add_chat_message(int(support_chat_id), SENDER_CLIENT,
                                   f"⚠️ Не удалось загрузить файл.\n{text}" if text else "⚠️ Не удалось загрузить файл.")
    else:
        # Только текст — сохраняем в историю
        await add_chat_message(int(support_chat_id), SENDER_CLIENT, text)

    # Рассылаем специалистам на обе платформы (TG + MAX) — добавляем офферы в БД
    notify_text = (
        f"💬 Новое обращение от <b>{_escape_html(client_name)}</b>\n"
        f"Тема: <b>{_escape_html(title)}</b>"
    )
    if text:
        short = text[:300] + ("…" if len(text) > 300 else "")
        notify_text += f"\n\n{_escape_html(short)}"

    for s in specs:
        if not isinstance(s, dict):
            continue
        s_id = int(s.get("id") or 0)
        s_role = str(s.get("role") or "specialist")

        # TG inline keyboard
        tg_markup: dict | None = None
        if s_role == "director" and profession != PROF_SERVICES:
            tg_markup = {"inline_keyboard": [
                [{"text": "📂 Открыть список", "callback_data": "ag_refresh"}],
            ]}
        else:
            tg_markup = {"inline_keyboard": [
                [
                    {"text": "✅ Принять", "callback_data": f"ag_offer_yes:{support_chat_id}"},
                    {"text": "❌ Отклонить", "callback_data": f"ag_offer_no:{support_chat_id}"},
                ],
            ]}

        # MAX inline keyboard
        if s_role == "director" and profession != PROF_SERVICES:
            max_kb = [
                [{"type": "callback", "text": "📂 Открыть список", "payload": "ag_refresh"}],
            ]
        else:
            max_kb = [
                [
                    {"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{support_chat_id}"},
                    {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{support_chat_id}"},
                ],
            ]

        try:
            tg_mid, _max_mid = await notify_specialist_all_platforms(
                s, notify_text,
                tg_reply_markup=tg_markup,
                max_keyboard_buttons=max_kb,
            )
        except Exception as e:
            logger.error("cross-notify error for spec %s: %s", s_id, e)
            tg_mid = 0

        # Сохраняем оффер (если не директор-наблюдатель)
        if not (s_role == "director" and profession != PROF_SERVICES):
            tg_to = int(s.get("tg_user_id") or 0)
            try:
                await add_chat_offer(int(support_chat_id), s_id, tg_to, tg_mid)
            except Exception as e:
                logger.error("add_chat_offer error: %s", e)

    # Ответ клиенту — через _send_or_edit, чтобы старое меню удалялось
    kb = InlineKeyboard(rows=[
        [InlineButton("📁 Мои обращения", callback_data="cc_my_chats")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, msg.chat_id,
                        "✅ Обращение отправлено. Ожидайте ответа специалиста.",
                        keyboard=kb)

    # Best-effort: удаляем исходное сообщение клиента с текстом обращения.
    user_mid = str(getattr(msg, "message_id", "") or "").strip()
    if user_mid:
        try:
            await ctx.api.delete_message(user_mid)
        except Exception:
            logger.debug("MAX delete user request message failed: mid=%s", user_mid, exc_info=True)

    asyncio.create_task(_schedule_pending_chat_reply_max(
        ctx.api,
        str(msg.chat_id),
        int(support_chat_id),
    ))

    _ctx_reset(ctx)
    _set_state(ctx, ST_IDLE)
    return True


async def create_client_chat_text_request_max(
    ctx: MaxBotContext,
    *,
    chat_id: str,
    max_user_id: str,
    text: str,
    message_id: str | None = None,
) -> bool:
    """Создать клиентское обращение с готовым текстом без ожидания ввода сообщения в MAX."""
    text = (text or "").strip()
    if not await _require_verified_for_requests(ctx, chat_id, message_id=message_id):
        _set_state(ctx, ST_IDLE)
        return True

    if not text:
        await send_temporary_message_max(ctx, chat_id, "Не удалось сформировать текст обращения.")
        _set_state(ctx, ST_IDLE)
        return True

    db_id = await _resolve_db_id(ctx, max_user_id) if max_user_id else ctx.user_data.get("_db_id", 0)
    if not db_id:
        await send_temporary_message_max(ctx, chat_id, "⚠️ Не удалось определить пользователя. Попробуйте /start.")
        _set_state(ctx, ST_IDLE)
        return True

    cc = _ctx_get(ctx)
    profession = int(cc.profession)
    title = (cc.category or cc.request_type or "Обращение").strip()
    client_name = await _client_display_name(db_id)

    director = await get_director_specialist()
    if profession == PROF_SERVICES:
        if not director:
            await ctx.api.send_message(chat_id, "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            _set_state(ctx, ST_IDLE)
            return True
        specs = [director]
    else:
        specs = await list_specialists_for_profession(profession) or []
        if director:
            did = int(director.get("id") or 0)
            if did and all(int(s.get("id") or 0) != did for s in specs):
                specs.append(director)
        if not specs:
            await ctx.api.send_message(chat_id, "⚠️ Сейчас не можем обработать обращение. Попробуйте позже.")
            _set_state(ctx, ST_IDLE)
            return True

    support_chat_id = await create_support_chat_request(
        internal_user_id=db_id,
        profession=profession,
        category=cc.category,
        request_type=cc.request_type,
        ensure_user_exists=False,
        source="max",
    )
    await add_chat_message(int(support_chat_id), SENDER_CLIENT, text)

    notify_text = (
        f"💬 Новое обращение от <b>{_escape_html(client_name)}</b>\n"
        f"Тема: <b>{_escape_html(title)}</b>\n\n"
        f"{_escape_html(text[:300] + ('…' if len(text) > 300 else ''))}"
    )

    for s in specs:
        if not isinstance(s, dict):
            continue
        s_id = int(s.get("id") or 0)
        s_role = str(s.get("role") or "specialist")

        if s_role == "director" and profession != PROF_SERVICES:
            tg_markup = {"inline_keyboard": [[{"text": "📂 Открыть список", "callback_data": "ag_refresh"}]]}
            max_kb = [[{"type": "callback", "text": "📂 Открыть список", "payload": "ag_refresh"}]]
        else:
            tg_markup = {"inline_keyboard": [[
                {"text": "✅ Принять", "callback_data": f"ag_offer_yes:{support_chat_id}"},
                {"text": "❌ Отклонить", "callback_data": f"ag_offer_no:{support_chat_id}"},
            ]]}
            max_kb = [[
                {"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{support_chat_id}"},
                {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{support_chat_id}"},
            ]]

        try:
            tg_mid, _max_mid = await notify_specialist_all_platforms(
                s,
                notify_text,
                tg_reply_markup=tg_markup,
                max_keyboard_buttons=max_kb,
            )
        except Exception as e:
            logger.error("cross-notify error for spec %s: %s", s_id, e)
            tg_mid = 0

        if not (s_role == "director" and profession != PROF_SERVICES):
            tg_to = int(s.get("tg_user_id") or 0)
            try:
                await add_chat_offer(int(support_chat_id), s_id, tg_to, tg_mid)
            except Exception as e:
                logger.error("add_chat_offer error: %s", e)

    kb = InlineKeyboard(rows=[
        [InlineButton("📁 Мои обращения", callback_data="cc_my_chats")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(
        ctx,
        chat_id,
        "✅ Обращение отправлено. Ожидайте ответа специалиста.",
        keyboard=kb,
        message_id=message_id,
    )

    _ctx_reset(ctx)
    _set_state(ctx, ST_IDLE)
    return True


# ══════════════════════════════════════════════════════════════════════
# Мои обращения (список)
# ══════════════════════════════════════════════════════════════════════

async def handle_my_chats(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_my_chats / my_chats"""
    if not cb.user:
        return
    await _clear_other_client_notices_for_callback(ctx, cb)

    max_uid = str(cb.user.user_id)
    db_id = await _resolve_db_id(ctx, max_uid)
    u = await get_user_by_internal_id(int(db_id)) if db_id else {}
    identity = await resolve_partner_identity_by_phone(
        internal_user_id=int(db_id) if db_id else None,
        max_user_id=int(max_uid),
        phone=(u or {}).get("phone"),
    )
    canonical_uid = int(identity.get("canonical_user_id") or 0) or int(db_id or 0)
    partner_chat_id = None
    if canonical_uid > 0:
        try:
            partner_chat_id = await ensure_partner_entrypoint_chat(
                partner_user_id=canonical_uid,
                max_user_id=int(max_uid),
                phone=identity.get("partner_phone") or (u or {}).get("phone"),
                source="max",
            )
        except Exception as _pce:
            logger.warning("MAX ensure_partner_entrypoint_chat failed: %s", _pce)
    logger.info("MAX partner_my_chats: max_user_id=%s current_internal_user_id=%s current_phone=%s partner_detected=%s partner_specialist_id=%s canonical_user_id=%s ensured_chat_id=%s", max_uid, db_id, (u or {}).get("phone"), identity.get("partner_detected"), identity.get("partner_specialist_id"), canonical_uid or None, partner_chat_id)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    # Партнёры (profession=5) не проходят обычную верификацию клиента — пропускаем проверку.
    if not identity.get("partner_detected"):
        if not await _require_verified_for_requests(ctx, chat_id, message_id=mid):
            return

    if canonical_uid <= 0:
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(
            ctx,
            chat_id,
            "Не найден внутренний профиль пользователя. Обновите телефон в профиле и повторите.",
            keyboard=kb,
            message_id=mid,
        )
        return

    chats = await list_chats_for_client(internal_user_id=canonical_uid)
    logger.info("MAX partner_my_chats: max_user_id=%s chats_count=%s", max_uid, len(chats))
    chats = sorted(
        chats,
        key=lambda ch: 0 if str(ch.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE else 1,
    )

    if not chats:
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id, "У вас нет активных обращений.", keyboard=kb, message_id=mid)
        return

    rows = []
    for ch in chats:
        prof = int(ch.get("profession") or -1)
        prof_label = (
            "🧾" if prof == PROF_ACCOUNTING
            else "🛠" if prof == PROF_SUPPORT
            else "🧰" if prof in (PROF_SERVICES, PROF_EQUIPMENT)
            else "👤"
        )
        st = ch.get("status")
        badge = "🟢" if st in ("active", "consultation") else "🟡" if st == "pending" else "⚪"
        title = ch.get("category") or ch.get("request_type") or f"Чат #{ch.get('id')}"
        rows.append([InlineButton(f"{badge} {prof_label} {title}", callback_data=f"cc_mychat:{ch['id']}")])

    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    await _send_or_edit(ctx, chat_id, "💬 Ваши обращения:\nВыберите чат:",
                        keyboard=InlineKeyboard(rows=rows), message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# Открытие отдельного чата
# ══════════════════════════════════════════════════════════════════════

async def handle_mychat_open(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_mychat:<chat_id>"""
    if not cb.user:
        return
    await _clear_other_client_notices_for_callback(ctx, cb)

    payload = cb.payload or ""
    logger.info("MAX handle_mychat_open: callback_data=%s max_user_id=%s", payload, cb.user.user_id if cb.user else None)
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    max_uid = str(cb.user.user_id)
    db_id = await _resolve_db_id(ctx, max_uid)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    chat = await get_chat_by_id(support_chat_id)
    identity = await resolve_partner_identity_by_phone(internal_user_id=int(db_id) if db_id else None, max_user_id=int(max_uid))
    access_uid = int(identity.get("canonical_user_id") or 0) if identity.get("partner_detected") else int(db_id or 0)
    if not chat or int(chat.get("client_user_id") or 0) != access_uid:
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id, "Чат не найден.", keyboard=kb, message_id=mid)
        return
    try:
        await delete_notice_max(
            max_api=ctx.api,
            max_user_id=int(cb.user.user_id),
            kind=NoticeKind.CLIENT_CHAT,
            entity_id=int(support_chat_id),
        )
    except Exception:
        logger.debug("best-effort delete current MAX client notice failed", exc_info=True)

    text = (
        f"💬 Чат\n"
        f"Тип: {chat.get('request_type') or '—'}\n"
        f"Категория: {chat.get('category') or '—'}\n"
        f"Статус: {status_ru(chat.get('status'))}\n\n"
        "✍️ Напишите ответ прямо здесь (кнопка «Написать здесь»)\n"
        "или откройте переписку в мини-приложении (кнопка «Ответить»)."
    )

    rows = []
    st = chat.get("status")
    if st != "closed":
        webapp_btn = await _chat_webapp_button(
            support_chat_id, role="client", actor_id=str(db_id), text="💬 Ответить")
        rows.append([webapp_btn])
        rows.append([InlineButton("✍️ Написать здесь", callback_data=f"cc_reply:{support_chat_id}")])
    is_partner_director_chat = str(chat.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE
    if st not in ("closed", "consultation") and not is_partner_director_chat:
        rows.append([InlineButton("✅ Завершить обращение", callback_data=f"cc_close:{support_chat_id}")])
    rows.append([InlineButton("📂 Мои обращения", callback_data="cc_my_chats")])
    rows.append([InlineButton("🔙 В меню", callback_data="back_to_main")])

    await _send_or_edit(ctx, chat_id, text, keyboard=InlineKeyboard(rows=rows), message_id=mid)



# ══════════════════════════════════════════════════════════════════════
# «Написать» на экране ввода — создаём чат и открываем WebApp
# ══════════════════════════════════════════════════════════════════════

async def handle_open_chat_btn(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_open_chat:<profession> — создаёт обращение и возвращает WebApp-кнопку."""
    if not cb.user:
        return

    payload = cb.payload or ""
    profession = int(payload.split(":", 1)[1]) if ":" in payload else PROF_SUPPORT

    max_uid = str(cb.user.user_id)
    chat_id_s = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    db_id = await _resolve_db_id(ctx, max_uid)
    if not db_id:
        await _send_or_edit(ctx, chat_id_s, "⚠️ Не удалось определить ваш профиль.", message_id=mid)
        return

    if not await _require_verified_for_requests(ctx, chat_id_s, message_id=mid):
        return

    cc = _ctx_get(ctx)
    cc.profession = profession
    _ctx_set(ctx, cc)

    support_chat_id = await create_support_chat_request(
        internal_user_id=db_id,
        profession=profession,
        category=cc.category,
        request_type=cc.request_type,
        source="max",
    )
    await add_chat_message(int(support_chat_id), SENDER_CLIENT, "Клиент открыл обращение")

    # Уведомляем специалистов
    client_name = await _client_display_name(db_id)
    title = (cc.category or cc.request_type or "Обращение").strip()
    notify_text = (
        f"💬 Новое обращение от <b>{_escape_html(client_name)}</b>\n"
        f"Тема: <b>{_escape_html(title)}</b>"
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
        s_id = int(s.get("id") or 0)
        s_role = str(s.get("role") or "specialist")
        if s_role == "director" and profession != PROF_SERVICES:
            tg_markup = {"inline_keyboard": [[{"text": "📂 Открыть список", "callback_data": "ag_refresh"}]]}
            max_kb = [[{"type": "callback", "text": "📂 Открыть список", "payload": "ag_refresh"}]]
        else:
            tg_markup = {"inline_keyboard": [[
                {"text": "✅ Принять", "callback_data": f"ag_offer_yes:{support_chat_id}"},
                {"text": "❌ Отклонить", "callback_data": f"ag_offer_no:{support_chat_id}"},
            ]]}
            max_kb = [[
                {"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{support_chat_id}"},
                {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{support_chat_id}"},
            ]]
        try:
            tg_mid, _ = await notify_specialist_all_platforms(
                s, notify_text, tg_reply_markup=tg_markup, max_keyboard_buttons=max_kb,
            )
        except Exception as e:
            logger.error("handle_open_chat_btn notify error spec %s: %s", s_id, e)
            tg_mid = 0
        if not (s_role == "director" and profession != PROF_SERVICES):
            tg_to = int(s.get("tg_user_id") or 0)
            try:
                await add_chat_offer(int(support_chat_id), s_id, tg_to, tg_mid)
            except Exception as e:
                logger.error("add_chat_offer error: %s", e)

    ctx.user_data["cc_reply_chat_id"] = support_chat_id
    _sup_reset(ctx)

    webapp_btn = await _chat_webapp_button(support_chat_id, role="client", actor_id=str(db_id), text="💬 Открыть чат")
    kb = InlineKeyboard(rows=[
        [webapp_btn],
        [InlineButton("✍️ Написать здесь", callback_data=f"cc_reply:{support_chat_id}")],
        [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(
        ctx, chat_id_s,
        "✅ Обращение создано.\n\n"
        "Нажмите «Открыть чат», чтобы написать в мини-приложении.\n"
        "Или нажмите «Написать здесь» — ответ придёт прямо в бот.",
        keyboard=kb, message_id=mid,
    )
    _set_state(ctx, ST_CC_WAIT_REPLY)


# ══════════════════════════════════════════════════════════════════════
# Написать сообщение в чат (прямо из бота)
# ══════════════════════════════════════════════════════════════════════

async def handle_cc_reply_btn(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_reply:<chat_id> — переводит в режим ввода ответного сообщения."""
    if not cb.user:
        return

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id_s = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    ctx.user_data["cc_reply_chat_id"] = support_chat_id
    _set_state(ctx, ST_CC_WAIT_REPLY)

    kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
    await _send_or_edit(ctx, chat_id_s, "Введите ваш ответ:", keyboard=kb, message_id=mid)


# ══════════════════════════════════════════════════════════════════════
# Закрытие обращения
# ══════════════════════════════════════════════════════════════════════

async def handle_cc_close(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_close:<chat_id> — запрос на закрытие."""
    if not cb.user:
        return
    await _clear_other_client_notices_for_callback(ctx, cb)

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    max_uid = str(cb.user.user_id)
    db_id = await _resolve_db_id(ctx, max_uid)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    chat = await get_chat_by_id(support_chat_id)
    identity = await resolve_partner_identity_by_phone(internal_user_id=int(db_id) if db_id else None, max_user_id=int(max_uid))
    access_uid = int(identity.get("canonical_user_id") or 0) if identity.get("partner_detected") else int(db_id or 0)
    if not chat or int(chat.get("client_user_id") or 0) != access_uid:
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id, "Чат не найден или он не ваш.", keyboard=kb, message_id=mid)
        return
    if str(chat.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE:
        kb = InlineKeyboard(rows=[[InlineButton("📂 Мои обращения", callback_data="cc_my_chats")]])
        await _send_or_edit(ctx, chat_id, "⛔ Постоянный чат с директором нельзя закрыть.", keyboard=kb, message_id=mid)
        return

    if str(chat.get("status")) == "closed":
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id, "Этот чат уже закрыт.", keyboard=kb, message_id=mid)
        return

    res = await request_chat_close(support_chat_id, by="client")
    if not res.get("ok"):
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id, "Не удалось обработать запрос.", keyboard=kb, message_id=mid)
        return

    if res.get("just_closed"):
        # Обе стороны согласны — закрываем
        await add_chat_message(support_chat_id, SENDER_CLIENT, "✅ Обращение завершено по согласию обеих сторон")

        # уведомляем специалиста на обеих платформах
        s_id = chat.get("specialist_id")
        if s_id:
            try:
                spec = await get_specialist_by_id(int(s_id))
                close_text = f"✅ Обращение закрыто по согласию обеих сторон (чат #{support_chat_id})."
                close_kb_max = [[{"type": "callback", "text": "⬅️ К списку чатов", "payload": "ag_refresh"}]]
                if spec:
                    await notify_specialist_all_platforms(
                        spec, close_text, max_keyboard_buttons=close_kb_max,
                        tg_reply_markup={"inline_keyboard": [[{"text": "⬅️ К списку чатов", "callback_data": "ag_back_to_chats"}]]},
                    )
            except Exception as e:
                logger.error("close notify error: %s", e)

        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id,
                            "✅ Обращение завершено.\n"
                            "Если нужна помощь — создайте новое обращение через меню.",
                            keyboard=kb, message_id=mid)
        _set_state(ctx, ST_IDLE)
        return

    # Ждём подтверждения специалиста — уведомляем на обеих платформах
    await add_chat_message(support_chat_id, SENDER_CLIENT, "🔒 Клиент запросил завершение обращения")

    s_id = chat.get("specialist_id")
    if s_id:
        try:
            spec = await get_specialist_by_id(int(s_id))
            close_req_text = f"🔒 Клиент запросил завершение обращения (чат #{support_chat_id}).\nПодтвердить закрытие?"
            close_req_kb_max = [
                [
                    {"type": "callback", "text": "✅ Подтвердить", "payload": f"ag_close_yes:{support_chat_id}"},
                    {"type": "callback", "text": "❌ Не закрывать", "payload": f"ag_close_cancel:{support_chat_id}"},
                ],
                [{"type": "callback", "text": "💬 Ответить", "payload": f"ag_chat:{support_chat_id}"}],
            ]
            close_req_kb_tg = {"inline_keyboard": [
                [
                    {"text": "✅ Подтвердить", "callback_data": f"ag_close_yes:{support_chat_id}"},
                    {"text": "❌ Не закрывать", "callback_data": f"ag_close_cancel:{support_chat_id}"},
                ],
                [{"text": "💬 Ответить", "callback_data": f"ag_chat:{support_chat_id}"}],
            ]}
            if spec:
                await notify_specialist_all_platforms(
                    spec, close_req_text,
                    tg_reply_markup=close_req_kb_tg,
                    max_keyboard_buttons=close_req_kb_max,
                )
        except Exception as e:
            logger.error("close request notify error: %s", e)

    kb = InlineKeyboard(rows=[
        [InlineButton("↩️ Продолжить общение", callback_data=f"cc_close_no:{support_chat_id}")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id,
                        "🔒 Запрос на завершение отправлен.\n"
                        "Чат закроется после подтверждения специалистом.",
                        keyboard=kb, message_id=mid)
    _set_state(ctx, ST_IDLE)


async def handle_cc_close_no(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_close_no:<chat_id> — отменить закрытие."""
    if not cb.user:
        return
    await _clear_other_client_notices_for_callback(ctx, cb)

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    max_uid = str(cb.user.user_id)
    db_id = await _resolve_db_id(ctx, max_uid)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    chat = await get_chat_by_id(support_chat_id)
    identity = await resolve_partner_identity_by_phone(internal_user_id=int(db_id) if db_id else None, max_user_id=int(max_uid))
    access_uid = int(identity.get("canonical_user_id") or 0) if identity.get("partner_detected") else int(db_id or 0)
    if not chat or int(chat.get("client_user_id") or 0) != access_uid:
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id, "Чат не найден.", keyboard=kb, message_id=mid)
        return
    if str(chat.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE:
        kb = InlineKeyboard(rows=[[InlineButton("📂 Мои обращения", callback_data="cc_my_chats")]])
        await _send_or_edit(ctx, chat_id, "⛔ Постоянный чат с директором нельзя закрыть.", keyboard=kb, message_id=mid)
        return

    if str(chat.get("status")) == "closed":
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(ctx, chat_id, "Этот чат уже закрыт.", keyboard=kb, message_id=mid)
        return

    await reset_chat_close_votes(support_chat_id)
    await add_chat_message(support_chat_id, SENDER_CLIENT, "↩️ Клиент отменил завершение")

    webapp_btn = await _chat_webapp_button(
        support_chat_id, role="client", actor_id=str(db_id), text="💬 Ответить")
    rows = [[webapp_btn]]
    if str(chat.get("request_type") or "").strip().lower() != PARTNER_DIRECTOR_REQUEST_TYPE:
        rows.append([InlineButton("✅ Завершить обращение", callback_data=f"cc_close:{support_chat_id}")])
    rows.extend([
        [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id, "↩️ Ок, продолжаем общение.",
                        keyboard=InlineKeyboard(rows=rows), message_id=mid)
    _set_state(ctx, ST_IDLE)


# ══════════════════════════════════════════════════════════════════════
# Обработка текста по state
# ══════════════════════════════════════════════════════════════════════

async def _on_reply_message(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Клиент пишет ответ прямо в MAX-боте. Отправляет сообщение в WebApp-чат через API."""
    support_chat_id = int(ctx.user_data.get("cc_reply_chat_id") or 0)
    if not support_chat_id:
        _set_state(ctx, ST_IDLE)
        return True

    file_info = _extract_max_file(msg)
    text = (msg.text or "").strip()

    if not text and not file_info:
        await send_temporary_message_max(ctx, msg.chat_id, "Отправьте текст и/или файл одним сообщением.")
        return True

    max_uid = str(msg.sender.user_id if msg.sender else "")
    db_id = await _resolve_db_id(ctx, max_uid)

    chat = await get_chat_by_id(support_chat_id)
    if not chat or int(chat.get("client_user_id") or 0) != int(db_id or 0):
        _set_state(ctx, ST_IDLE)
        return True

    if str(chat.get("status")) == "closed":
        await send_temporary_message_max(ctx, msg.chat_id, "Чат уже закрыт. Создайте новое обращение.")
        _set_state(ctx, ST_IDLE)
        return True

    if not chat.get("specialist_id") and str(chat.get("status")) == "pending":
        await send_temporary_message_max(ctx, msg.chat_id, "Заявка обрабатывается. Пожалуйста, подождите.")
        return True

    token = await create_chat_access_token(
        chat_id=int(support_chat_id),
        role="client",
        actor_id=str(db_id),
    )

    try:
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            if file_info:
                file_data = await ctx.api.download_file(file_info["url"])
                resp = await client.post(
                    f"{WEBAPP_URL}/api/chats/{int(support_chat_id)}/files?token={token}",
                    data={"sender": "0", "text": (text or "")[:4000]},
                    files={"file": (file_info["filename"], file_data, file_info["mime"])},
                )
                resp.raise_for_status()
            else:
                resp = await client.post(
                    f"{WEBAPP_URL}/api/chats/{int(support_chat_id)}/messages?token={token}",
                    json={"sender": 0, "text": text},
                )
                resp.raise_for_status()
    except Exception as e:
        logger.error("_on_reply_message WebApp API error: %s", e)
        await send_temporary_message_max(ctx, msg.chat_id, "⚠️ Не удалось отправить сообщение. Попробуйте позже.")
        return True

    ctx.user_data.pop("cc_reply_chat_id", None)
    _set_state(ctx, ST_IDLE)

    kb = InlineKeyboard(rows=[
        [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await send_temporary_message_max(ctx, msg.chat_id, "✅ Сообщение отправлено.", keyboard=kb)
    return True


async def handle_cc_text_by_state(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """
    Обрабатывает текстовые сообщения для клиентского чата.
    Возвращает True если обработано.
    """
    if not msg.sender:
        return False

    state = ctx.user_data.get("_state", ST_IDLE)

    if state == ST_CC_WAIT_CATEGORY:
        # В мастере техподдержки текст не ожидается — подсказываем выбрать кнопку
        await send_temporary_message_max(ctx, msg.chat_id, "Пожалуйста, выберите вариант из списка выше.")
        return True

    if state == ST_CC_WAIT_CONTENT:
        return await _on_content_message(ctx, msg)

    if state == ST_CC_WAIT_REPLY:
        return await _on_reply_message(ctx, msg)

    return False


# ══════════════════════════════════════════════════════════════════════
# Регистрация обработчиков
# ══════════════════════════════════════════════════════════════════════

async def handle_cc_upd_all(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_upd_all:<profession> — Подписание УПД, все документы."""
    if not cb.user:
        return

    payload = cb.payload or ""
    parts = payload.split(":")
    profession = int(parts[1]) if len(parts) > 1 else PROF_ACCOUNTING

    max_uid = str(cb.user.user_id or ctx.user_data.get("_max_uid") or "").strip()
    chat_id_s = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    db_id = await _resolve_db_id(ctx, max_uid)
    if not db_id:
        await _send_or_edit(ctx, chat_id_s, "⚠️ Не удалось определить ваш профиль.", message_id=mid)
        return

    cc = _ctx_get(ctx)
    cc.profession = profession
    cc.request_type = "Подписание УПД"
    cc.category = "Все"
    _ctx_set(ctx, cc)

    support_chat_id = await create_support_chat_request(
        internal_user_id=db_id,
        profession=int(profession),
        category="Все",
        request_type="Подписание УПД",
        source="max",
    )
    await add_chat_message(int(support_chat_id), SENDER_CLIENT, "Подписание УПД — все документы")

    username = str(cb.user.username or cb.user.name or f"MAX {max_uid}").strip()
    title = "Подписание УПД — Все"
    notify_text = (
        f"💬 Новое обращение от <b>{_escape_html(username)}</b>\n"
        f"Тема: <b>{_escape_html(title)}</b>"
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
        s_id = int(s.get("id") or 0)
        s_role = str(s.get("role") or "specialist")
        tg_markup: dict | None = None
        if s_role == "director" and profession != PROF_SERVICES:
            tg_markup = {"inline_keyboard": [[{"text": "📂 Открыть список", "callback_data": "ag_refresh"}]]}
            max_kb = [[{"type": "callback", "text": "📂 Открыть список", "payload": "ag_refresh"}]]
        else:
            tg_markup = {"inline_keyboard": [[
                {"text": "✅ Принять", "callback_data": f"ag_offer_yes:{support_chat_id}"},
                {"text": "❌ Отклонить", "callback_data": f"ag_offer_no:{support_chat_id}"},
            ]]}
            max_kb = [[
                {"type": "callback", "text": "✅ Принять", "payload": f"ag_offer_yes:{support_chat_id}"},
                {"type": "callback", "text": "❌ Отклонить", "payload": f"ag_offer_no:{support_chat_id}"},
            ]]
        try:
            tg_mid, _ = await notify_specialist_all_platforms(
                s, notify_text, tg_reply_markup=tg_markup, max_keyboard_buttons=max_kb,
            )
        except Exception as e:
            logger.error("handle_cc_upd_all notify error spec %s: %s", s_id, e)
            tg_mid = 0
        if not (s_role == "director" and profession != PROF_SERVICES):
            tg_to = int(s.get("tg_user_id") or 0)
            try:
                await add_chat_offer(int(support_chat_id), s_id, tg_to, tg_mid)
            except Exception as e:
                logger.error("add_chat_offer error: %s", e)

    kb = InlineKeyboard(rows=[
        [InlineButton("📁 Мои обращения", callback_data="cc_my_chats")],
        [InlineButton("🔙 В меню", callback_data="back_to_main")],
    ])
    await _send_or_edit(ctx, chat_id_s,
                        "✅ Обращение «Подписание УПД — Все» отправлено. Ожидайте ответа.",
                        keyboard=kb, message_id=mid)
    _sup_reset(ctx)
    _set_state(ctx, ST_IDLE)


async def handle_cc_upd_selective(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Callback: cc_upd_selective:<profession> — Подписание УПД, выборочно (клиент пишет сам)."""
    if not cb.user:
        return

    payload = cb.payload or ""
    parts = payload.split(":")
    profession = int(parts[1]) if len(parts) > 1 else PROF_ACCOUNTING

    chat_id_s = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    cc = _ctx_get(ctx)
    cc.profession = profession
    cc.request_type = "Подписание УПД"
    cc.category = "Выборочно"
    _ctx_set(ctx, cc)

    kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
    await _send_or_edit(
        ctx, chat_id_s,
        "✍️ Напишите, какие именно документы нужно подписать.\n\n"
        "Если нужно — прикрепите файл/скриншот.\n"
        "Отправьте одним сообщением.",
        keyboard=kb, message_id=mid,
    )
    _set_state(ctx, ST_CC_WAIT_CONTENT)


def register_client_chat_handlers(bot) -> None:
    """Подключить обработчики клиентского чата к MaxBot."""

    # Вход в чат (бухгалтерия/поддержка)
    bot.register_callback("client_chat_start:*", handle_client_chat_start)

    # Выбор типа обращения
    bot.register_callback("cc_type:*", handle_cc_type)

    # Мои обращения
    bot.register_callback("cc_my_chats", handle_my_chats)
    bot.register_callback("my_chats", handle_my_chats)

    # Открытие чата
    bot.register_callback("cc_mychat:*", handle_mychat_open)

    # Подменю тех. поддержки (ОФД / Продление / 1С)
    bot.register_callback("cc_sub:*", handle_cc_sub)

    # Категории / мастер техподдержки
    bot.register_callback("cc_cat:*", handle_cc_category)
    bot.register_callback("cc_back:*", handle_cc_back)

    # Подписание УПД
    bot.register_callback("cc_upd_all:*", handle_cc_upd_all)
    bot.register_callback("cc_upd_selective:*", handle_cc_upd_selective)

    # Открыть чат через WebApp (с экрана ввода первого сообщения)
    bot.register_callback("cc_open_chat:*", handle_open_chat_btn)

    # Написать в чат
    bot.register_callback("cc_reply:*", handle_cc_reply_btn)

    # Закрытие
    bot.register_callback("cc_close:*", handle_cc_close)
    bot.register_callback("cc_close_no:*", handle_cc_close_no)

    logger.info("MAX-бот: обработчики клиентского чата подключены")
