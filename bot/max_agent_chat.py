"""
max_agent_chat.py — Агентский/Специалистский чат для MAX-бота.

Перенос логики из bot_agent_chat.py:
- Авторизация специалиста по телефону (текстовый ввод, т.к. в MAX нет request_contact)
- Список чатов / заявок (свои для специалиста, все отделы для директора)
- Просмотр чата с WebApp-кнопкой
- Принять / отклонить заявку
- Закрытие обращения (двустороннее)
- Переназначение (директор)
- Фоновые уведомления о новых заявках
"""

import re
import time
import logging
import os
import zlib
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

from max_api import MaxMessage, MaxCallback, InlineKeyboard, InlineButton
from max_bot_main import MaxBot, MaxBotContext
from max_registration import _send_or_edit, _set_state, _show_main_menu, _resolve_db_id, _resolve_chat_id, ST_IDLE, send_temporary_message_max
from notice_center import (
    NoticeCluster,
    NoticeKind,
    clear_other_notices_for_user_max,
    delete_notice_cluster_max,
    delete_notice_max,
    upsert_notice_max,
)

import sys
from pathlib import Path

_bot_dir = Path(__file__).resolve().parent
_project_dir = _bot_dir.parent
sys.path.insert(0, str(_project_dir))

from db import (
    get_specialist_by_max,
    get_specialist_by_phone,
    is_partner_specialist,
    link_specialist_max_user,
    set_specialist_phone_verified,
    list_chats_for_specialist,
    list_chats_for_director,
    get_director_chat_counts,
    get_chat_by_id,
    try_accept_chat,
    mark_chat_declined,
    count_declines_for_chat,
    count_active_specialists_for_profession,
    get_least_busy_specialist_for_profession,
    force_assign_chat,
    list_chat_offers,
    delete_chat_offers,
    add_chat_message,
    request_chat_close,
    reset_chat_close_votes,
    list_specialists_for_profession,
    get_specialist_by_id,
    get_payment_by_payment_id,
    create_chat_access_token,
    count_pending_unassigned,
    list_ready_specialists_for_notify_max,
    ensure_specialist_max_column,
    get_or_create_specialist_ref_code,
)
from qr_utils import generate_qr_file_hashed

logger = logging.getLogger(__name__)

from cross_notify import notify_specialist_tg
from webapp_settings import WEBAPP_URL, MAX_BOT_NAME, MAX_REF_BOT_NAME, TG_REF_BOT_USERNAME

# ── Состояния ─────────────────────────────────────────────────────────
ST_AG_WAIT_PHONE = "ag_wait_phone"
ST_AG_MENU = "ag_menu"
ST_AG_CLOSE_CONFIRM = "ag_close_confirm"
AG_LAST_CHAT_MENU_MID_KEY = "_ag_last_chat_menu_mid"
AG_CHATS_PAGE_SIZE = 5

SENDER_CLIENT = 0
SENDER_SPEC = 1

MAX_REF_LINK_BOT_NAME = (MAX_REF_BOT_NAME or MAX_BOT_NAME or "").strip().lstrip("@")


async def _send_client_notice(
    api,
    max_uid_client: int,
    chat_id: int,
    text: str,
    keyboard=None,
) -> None:
    """Отправить/обновить клиентский notice в MAX через общий notice_center."""
    await upsert_notice_max(
        max_api=api,
        max_user_id=int(max_uid_client),
        kind=NoticeKind.CLIENT_CHAT,
        entity_id=int(chat_id),
        text=text,
        keyboard=keyboard,
    )


# ── AgentCtx ──────────────────────────────────────────────────────────
@dataclass
class AgentCtx:
    specialist_id: int
    max_user_id: int
    profession: int
    role: str = "specialist"


def _norm_phone(phone: str) -> str:
    s = re.sub(r"\D+", "", phone or "")
    if s.startswith("8") and len(s) == 11:
        s = "7" + s[1:]
    if s.startswith("9") and len(s) == 10:
        s = "7" + s
    return s


def _short(s: str, n: int = 28) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _paginate_items(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], page, total_pages


def _get_agent_ctx(ctx: MaxBotContext) -> Optional[AgentCtx]:
    d = ctx.user_data.get("agent_ctx")
    if not isinstance(d, dict):
        return None
    try:
        return AgentCtx(
            specialist_id=int(d["specialist_id"]),
            max_user_id=int(d["max_user_id"]),
            profession=int(d["profession"]),
            role=str(d.get("role") or "specialist"),
        )
    except Exception:
        return None


def _set_agent_ctx(ctx: MaxBotContext, row: Dict[str, Any], max_user_id: int) -> None:
    profession = int(row.get("profession") or 0)
    role = row.get("role") or "specialist"
    # profession=2 всегда director, даже если role в БД ещё не обновлён
    if profession == 2 or int(row.get("is_director") or 0) == 1:
        role = "director"
    ctx.user_data["agent_ctx"] = {
        "specialist_id": int(row["id"]),
        "max_user_id": int(max_user_id),
        "profession": profession,
        "role": role,
    }


async def _clear_specialist_service_notices_for_callback(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Best-effort очистка остальных specialist service notices при callback."""
    if not cb.user:
        return
    max_uid = int(cb.user.user_id)
    current_mid = cb.message.message_id if cb.message else None
    clusters = [
        NoticeCluster.SPECIALIST_PENDING_NOTICES,
        NoticeCluster.SPECIALIST_CHAT_NOTICES,
    ]
    try:
        current_cluster = None
        for cluster in clusters:
            matched = await clear_other_notices_for_user_max(
                max_api=ctx.api,
                max_user_id=max_uid,
                cluster=cluster,
                current_message_id=current_mid,
            )
            if matched:
                current_cluster = cluster

        for cluster in clusters:
            if cluster == current_cluster:
                continue
            await delete_notice_cluster_max(
                max_api=ctx.api,
                max_user_id=max_uid,
                cluster=cluster,
            )
    except Exception:
        logger.debug("best-effort specialist service notice cleanup failed for %s", max_uid, exc_info=True)


async def _chat_webapp_button(
    support_chat_id: int,
    role: str,
    actor_id: str | int,
    text: str = "💬 Ответить",
) -> InlineButton:
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


# ══════════════════════════════════════════════════════════════════════
# Клавиатуры
# ══════════════════════════════════════════════════════════════════════

def _kb_dir_sections(counts: dict | None = None, *, allow_client_menu: bool = False) -> InlineKeyboard:
    counts = counts or {}

    def _fmt(key) -> str:
        d = counts.get(key) or {}
        p = int(d.get("pending") or 0)
        a = int(d.get("active") or 0) + int(d.get("consultation") or 0)
        return f" (🟡{p} 🟢{a})" if (p or a) else " (0)"

    rows = [
        [InlineButton(f"📌 Все заявки{_fmt('all')}", callback_data="ag_dir_prof:all")],
        [InlineButton(f"🧾 Бухгалтерия{_fmt(0)}", callback_data="ag_dir_prof:0")],
        [InlineButton(f"🛠 Техподдержка{_fmt(1)}", callback_data="ag_dir_prof:1")],
        [InlineButton(f"💼 Услуги{_fmt(2)}", callback_data="ag_dir_prof:2")],
        [InlineButton(f"💼 Оборудование{_fmt(3)}", callback_data="ag_dir_prof:3")],
        [InlineButton(f"📄 Разрешительная документация{_fmt(4)}", callback_data="ag_dir_prof:4")],
    ]
    if allow_client_menu:
        rows.append([InlineButton("🔙 В меню клиента", callback_data="back_to_main")])
    return InlineKeyboard(rows=rows)


def _kb_chats(
    chat_rows,
    header_rows=None,
    *,
    allow_client_menu: bool = False,
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboard:
    rows = []
    if header_rows:
        rows.extend(header_rows)
    for ch in chat_rows:
        client = ch.get("client_name") or f"Клиент {ch.get('client_user_id')}"
        topic = ch.get("category") or ch.get("request_type") or f"Чат #{ch.get('id')}"
        status = ch.get("status")
        badge = "🟢" if status in ("active", "consultation") else "🟡" if status == "pending" else "⚪"
        rows.append([
            InlineButton(f"{badge} {client} — {_short(topic)}", callback_data=f"ag_chat:{ch['id']}")
        ])
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineButton("⬅️", callback_data=f"ag_page:{page - 1}"))
        nav_row.append(InlineButton(f"{page}/{total_pages}", callback_data="ag_page_noop"))
        if page < total_pages:
            nav_row.append(InlineButton("➡️", callback_data=f"ag_page:{page + 1}"))
        rows.append(nav_row)
    rows.append([InlineButton("🎯 Мой реферальный QR", callback_data="ag_ref_qr")])
    rows.append([InlineButton("🔄 Обновить", callback_data="ag_refresh")])
    if allow_client_menu:
        rows.append([InlineButton("🔙 В меню клиента", callback_data="back_to_main")])
    return InlineKeyboard(rows=rows)


async def _kb_chat_menu(
    support_chat_id: int,
    actor_id: int | str,
    status: str | None = None,
    *,
    role: str = "agent",
    allow_client_menu: bool = False,
) -> InlineKeyboard:
    st = (status or "").strip().lower()
    rows = []

    if st == "closed":
        rows.append([await _chat_webapp_button(support_chat_id, role=role, actor_id=actor_id)])
        rows.append([InlineButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")])
        return InlineKeyboard(rows=rows)

    rows.append([await _chat_webapp_button(support_chat_id, role=role, actor_id=actor_id)])

    if st != "consultation":
        rows.append([InlineButton("✅ Завершить обращение", callback_data=f"ag_close:{support_chat_id}")])

    rows.append([InlineButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")])

    if allow_client_menu:
        rows.append([InlineButton("🔙 В меню клиента", callback_data="back_to_main")])
        if st not in ("closed", "consultation"):
            rows.append([InlineButton("🔁 Сменить специалиста", callback_data=f"ag_reassign:{support_chat_id}")])

    return InlineKeyboard(rows=rows)


# ══════════════════════════════════════════════════════════════════════
# Рендер списка чатов
# ══════════════════════════════════════════════════════════════════════

async def _render_chat_list(ctx: MaxBotContext, chat_id: str, message_id: str | None = None) -> None:
    agent = _get_agent_ctx(ctx)
    if not agent:
        await _show_main_menu(ctx, chat_id, "Не удалось определить специалиста.")
        return

    # заглушаем уведомления на 2 минуты
    try:
        bd = ctx.bot_data if hasattr(ctx, "bot_data") else {}
        mute = bd.get("spec_mute_until_ts")
        if not isinstance(mute, dict):
            mute = {}
            bd["spec_mute_until_ts"] = mute
        mute[int(agent.max_user_id)] = time.time() + 120
    except Exception:
        pass
    try:
        await delete_notice_max(
            max_api=ctx.api,
            max_user_id=int(agent.max_user_id),
            kind=NoticeKind.SPECIALIST_PENDING,
            entity_id=int(agent.max_user_id),
        )
    except Exception:
        logger.debug("best-effort delete specialist pending notice failed for %s", agent.max_user_id, exc_info=True)

    async def _render_list_screen(text_: str, kb_: InlineKeyboard) -> None:
        prev_mid = str(ctx.user_data.get(AG_LAST_CHAT_MENU_MID_KEY) or "").strip()
        used_mid = await _send_or_edit(ctx, chat_id, text_, keyboard=kb_, message_id=message_id)
        final_mid = str(used_mid or message_id or "").strip()
        if final_mid:
            ctx.user_data[AG_LAST_CHAT_MENU_MID_KEY] = final_mid
        if prev_mid and final_mid and prev_mid != final_mid:
            result = await ctx.api.delete_message(prev_mid)
            if not result.get("ok"):
                if int(result.get("http_status") or 0) == 403:
                    logger.debug("skip forbidden delete previous MAX menu: mid=%s", prev_mid)
                else:
                    logger.warning("failed to delete previous MAX menu: mid=%s result=%s", prev_mid, result)

    text = "📂 Ваши чаты / заявки:\n\n🟡 — ожидают принятия\n🟢 — ваши активные\n\nВыберите:"

    if agent.role != "director":
        chats = await list_chats_for_specialist(agent.specialist_id)
        page = int(ctx.user_data.get("ag_chats_page") or 1)
        page_items, page, total_pages = _paginate_items(chats, page, AG_CHATS_PAGE_SIZE)
        ctx.user_data["ag_chats_page"] = page
        kb = _kb_chats(
            page_items,
            allow_client_menu=False,
            page=page,
            total_pages=total_pages,
        )
        await _render_list_screen(text, kb)
        _set_state(ctx, ST_AG_MENU)
        return

    # директор — выбор отдела
    sel = ctx.user_data.get("ag_dir_prof")
    if not sel:
        try:
            counts = await get_director_chat_counts()
        except Exception:
            counts = {}
        await _render_list_screen(
            "Выберите раздел заявок:",
            _kb_dir_sections(counts, allow_client_menu=True),
        )
        _set_state(ctx, ST_AG_MENU)
        return

    profession = None if sel == "all" else int(sel)
    chats = await list_chats_for_director(profession=profession)
    page = int(ctx.user_data.get("ag_chats_page") or 1)
    page_items, page, total_pages = _paginate_items(chats, page, AG_CHATS_PAGE_SIZE)
    ctx.user_data["ag_chats_page"] = page
    kb = _kb_chats(
        page_items,
        header_rows=[[InlineButton("⬅️ К отделам", callback_data="ag_dir_sections")]],
        allow_client_menu=True,
        page=page,
        total_pages=total_pages,
    )
    await _render_list_screen(text, kb)
    _set_state(ctx, ST_AG_MENU)


async def _show_chat_by_id(ctx: MaxBotContext, chat_id: str, support_chat_id: int, message_id: str | None = None) -> None:
    chat = await get_chat_by_id(support_chat_id)
    if not chat:
        await _send_or_edit(ctx, chat_id, "Чат не найден.", message_id=message_id)
        return

    agent = _get_agent_ctx(ctx)
    if not agent:
        return

    title = chat.get("client_name") or f"Клиент {chat.get('client_user_id')}"
    phone = (chat.get("client_phone") or "").strip()
    inn = (chat.get("client_inn") or "").strip()
    we_added = bool(chat.get("client_we_added"))
    is_director = agent.role == "director"

    if is_director:
        phone_line = f"Телефон: {phone or '—'}"
        inn_line = f"ИНН: {inn or '—'}"
    else:
        phone_line = "Телефон: указан" if phone else "Телефон: не указан"
        inn_line = "ИНН: указан" if inn else "ИНН: не указан"

    text = (
        f"💬 Чат: {title}\n"
        f"Тип: {chat.get('request_type') or '—'}\n"
        f"Категория: {chat.get('category') or '—'}\n"
        f"Статус: {_status_ru(chat.get('status'))}\n\n"
        f"{'Наш клиент' if we_added else 'Неизвестный клиент'}\n"
        f"{phone_line}\n"
        f"{inn_line}\n\n"
        "Нажмите «Ответить» для перехода в переписку."
    )

    role = "director" if agent.role == "director" else "agent"
    kb = await _kb_chat_menu(
        support_chat_id,
        actor_id=agent.max_user_id,
        status=chat.get("status"),
        role=role,
        allow_client_menu=(agent.role == "director"),
    )
    await _send_or_edit(ctx, chat_id, text, keyboard=kb, message_id=message_id)


def _status_ru(status: str | None) -> str:
    s = (status or "").strip().lower()
    return {
        "pending": "Ожидает принятия",
        "active": "В работе",
        "test": "Тест",
        "closed": "Закрыто",
        "declined": "Отклонено",
        "consultation": "Консультация",
    }.get(s, status or "—")


# ══════════════════════════════════════════════════════════════════════
# Вход — /agent
# ══════════════════════════════════════════════════════════════════════

async def handle_agent_start(ctx: MaxBotContext, msg_or_cb) -> None:
    """Вход в режим специалиста. Вызывается по команде /agent или callback."""
    if isinstance(msg_or_cb, MaxCallback):
        user = msg_or_cb.user
        chat_id = msg_or_cb.message.chat_id if msg_or_cb.message else ""
        mid = msg_or_cb.message.message_id if msg_or_cb.message else None
    else:
        user = msg_or_cb.sender
        chat_id = msg_or_cb.chat_id
        mid = None

    if not user:
        return

    max_uid = int(user.user_id)
    username = (user.username or "").strip()

    spec = await get_specialist_by_max(max_user_id=max_uid, max_username=username or None)
    if not spec or is_partner_specialist(spec):
        ctx.user_data["ag_login_max_uid"] = max_uid
        ctx.user_data["ag_login_username"] = username
        kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
        await _send_or_edit(
            ctx, chat_id,
            "Введите номер телефона, который указан у вас в базе специалистов.\n"
            "Формат: +7XXXXXXXXXX или 8XXXXXXXXXX",
            keyboard=kb,
            message_id=mid,
        )
        _set_state(ctx, ST_AG_WAIT_PHONE)
        return

    # Привязываем max_user_id
    try:
        await link_specialist_max_user(int(spec["id"]), max_uid, username or None)
        spec["max_user_id"] = max_uid
    except Exception as e:
        logger.error("link_specialist_max_user error: %s", e)

    _set_agent_ctx(ctx, spec, max_uid)

    is_verified = int(spec.get("phone_verified") or 0) == 1

    if is_verified:
        await _render_chat_list(ctx, chat_id, mid)
        return

    # Верификация по телефону (текстовый ввод, т.к. MAX не поддерживает request_contact)
    kb = InlineKeyboard(rows=[[InlineButton("🔙 В меню", callback_data="back_to_main")]])
    await _send_or_edit(
        ctx, chat_id,
        "Для входа в режим специалиста введите ваш номер телефона (как в базе).\n"
        "Формат: +7XXXXXXXXXX или 8XXXXXXXXXX",
        keyboard=kb,
        message_id=mid,
    )
    _set_state(ctx, ST_AG_WAIT_PHONE)


# ══════════════════════════════════════════════════════════════════════
# Верификация телефона (текстовый ввод)
# ══════════════════════════════════════════════════════════════════════

async def _on_agent_phone(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Обработка ввода телефона специалиста."""
    agent = _get_agent_ctx(ctx)

    text = (msg.text or "").strip()
    if not text:
        await send_temporary_message_max(ctx, msg.chat_id, "Введите номер телефона.")
        return True

    entered = _norm_phone(text)
    if not entered:
        await send_temporary_message_max(ctx, msg.chat_id, "Введите телефон в формате +7XXXXXXXXXX или 8XXXXXXXXXX.")
        return True

    spec = None
    if agent:
        # если контекст специалиста уже есть — проверяем по привязанному профилю
        spec = await get_specialist_by_max(
            max_user_id=agent.max_user_id,
            max_username=(msg.sender.username if msg.sender else "") or "",
        )
    if not spec:
        # первичный вход в MAX: ищем только по телефону (username может отсутствовать)
        spec = await get_specialist_by_phone(entered)

    if not spec:
        await ctx.api.send_message(
            msg.chat_id,
            "Вы не найдены в базе специалистов. Продолжайте как клиент через главное меню.",
        )
        _set_state(ctx, ST_IDLE)
        return True

    if is_partner_specialist(spec):
        logger.info(
            "MAX agent login blocked for partner specialist_id=%s profession=%s max_user_id=%s",
            spec.get("id"),
            spec.get("profession"),
            msg.sender.user_id if msg.sender else None,
        )
        await ctx.api.send_message(
            msg.chat_id,
            "Для партнёра доступен клиентский режим. Используйте меню клиента и «Мои обращения».",
        )
        _set_state(ctx, ST_IDLE)
        return True

    expected = _norm_phone(spec.get("phone") or "")

    if not expected:
        await ctx.api.send_message(
            msg.chat_id,
            "Для вашего аккаунта не задан номер телефона в базе. Обратитесь к администратору.",
        )
        _set_state(ctx, ST_IDLE)
        return True

    if entered != expected:
        await ctx.api.send_message(
            msg.chat_id,
            "Номер телефона не совпал. Попробуйте ещё раз или обратитесь к администратору.",
        )
        return True

    # ок — помечаем verified
    await set_specialist_phone_verified(int(spec["id"]), True)
    await link_specialist_max_user(
        int(spec["id"]),
        int(ctx.user_data.get("ag_login_max_uid") or (agent.max_user_id if agent else (msg.sender.user_id if msg.sender else 0))),
        (msg.sender.username if msg.sender else None),
    )
    _set_agent_ctx(
        ctx,
        spec,
        int(ctx.user_data.get("ag_login_max_uid") or (agent.max_user_id if agent else (msg.sender.user_id if msg.sender else 0))),
    )
    ctx.user_data.pop("ag_login_max_uid", None)
    ctx.user_data.pop("ag_login_username", None)

    await send_temporary_message_max(ctx, msg.chat_id, "✅ Телефон подтверждён. Режим специалиста активирован.")
    await _render_chat_list(ctx, msg.chat_id)
    return True


# ══════════════════════════════════════════════════════════════════════
# Обновить / Отделы
# ══════════════════════════════════════════════════════════════════════

async def handle_ag_refresh(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)
    agent = _get_agent_ctx(ctx)
    if not agent:
        await handle_agent_start(ctx, cb)
        return
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    await _render_chat_list(ctx, chat_id, mid)


async def handle_ag_page(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    payload = cb.payload or ""
    try:
        page = int(payload.split(":", 1)[1])
    except Exception:
        page = 1

    ctx.user_data["ag_chats_page"] = max(1, page)

    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    await _render_chat_list(ctx, chat_id, mid)


async def handle_ag_page_noop(ctx: MaxBotContext, cb: MaxCallback) -> None:
    return


async def handle_ag_dir_prof(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    payload = cb.payload or ""
    prof = payload.split(":", 1)[1] if ":" in payload else "all"
    ctx.user_data["ag_dir_prof"] = prof
    ctx.user_data["ag_chats_page"] = 1
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    await _render_chat_list(ctx, chat_id, mid)


async def handle_ag_dir_sections(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    ctx.user_data.pop("ag_dir_prof", None)
    ctx.user_data["ag_chats_page"] = 1
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    await _render_chat_list(ctx, chat_id, mid)


async def handle_ag_back_to_chats(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None
    await _render_chat_list(ctx, chat_id, mid)


async def handle_ag_ref_qr(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    agent = _get_agent_ctx(ctx)
    if not agent:
        await handle_agent_start(ctx, cb)
        return

    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    max_uid = str(cb.user.user_id or ctx.user_data.get("_max_uid") or "").strip()
    if not chat_id and not max_uid:
        logger.error("handle_ag_ref_qr: no recipient resolved (chat_id/max_uid empty)")
        return
    mid = cb.message.message_id if cb.message else None

    data = ((cb.payload or "") if cb else "").strip()
    selected_platform = ""
    if ":" in data:
        selected_platform = data.split(":", 1)[1].strip().lower()

    if selected_platform not in ("telegram", "max"):
        kb = InlineKeyboard(rows=[
            [InlineButton("Telegram", callback_data="ag_ref_qr:telegram")],
            [InlineButton("MAX", callback_data="ag_ref_qr:max")],
            [InlineButton("📂 К заявкам", callback_data="ag_refresh")],
        ])
        await ctx.api.send_message(chat_id, "Выберите платформу для реферального QR:", keyboard=kb)
        return

    spec = await get_specialist_by_id(agent.specialist_id)
    owner_name = (spec or {}).get("name") or f"Специалист #{agent.specialist_id}"
    ref_code = await get_or_create_specialist_ref_code(agent.specialist_id, owner_name=owner_name)
    if not ref_code:
        await ctx.api.send_message(chat_id, "Не удалось создать реферальный код.")
        return

    if selected_platform == "telegram":
        if not TG_REF_BOT_USERNAME:
            await ctx.api.send_message(chat_id, "⚠️ Не настроен TG_REF_BOT_USERNAME для реферальных ссылок.")
            return
        referral_url = f"https://t.me/{TG_REF_BOT_USERNAME}?start=ref_{ref_code}"
        qr_namespace = "tg_specialist_ref"
    else:
        if not MAX_REF_LINK_BOT_NAME:
            await ctx.api.send_message(chat_id, "⚠️ Не настроен MAX_BOT_NAME для реферальных ссылок.")
            return
        referral_url = f"https://max.ru/{MAX_REF_LINK_BOT_NAME}?start=ref_{ref_code}"
        qr_namespace = "max_specialist_ref"

    back_kb = InlineKeyboard(rows=[[InlineButton("📂 Вернуться к заявкам", callback_data="ag_refresh")]])
    try:
        qr_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "qrs")
        os.makedirs(qr_dir, exist_ok=True)
        qr_path = generate_qr_file_hashed(referral_url, out_dir=qr_dir, namespace=qr_namespace)
        with open(qr_path, "rb") as f:
            await ctx.api.send_photo(
                chat_id,
                f.read(),
                user_id=max_uid,
                caption=(
                    "🎯 Ваш реферальный QR для раздела «Мои заявки».\n"
                    "Поделитесь им с клиентом — ссылка активирует ref-код автоматически.\n\n"
                    f"Ссылка: {referral_url}"
                ),
                filename=os.path.basename(qr_path),
                keyboard=back_kb,
            )
    except Exception:
        await ctx.api.send_message(chat_id, f"Ваша реферальная ссылка:\n{referral_url}", keyboard=back_kb)
    if mid:
        try:
            await ctx.api.delete_message(str(mid))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# Открыть конкретный чат
# ══════════════════════════════════════════════════════════════════════

async def handle_ag_open_chat(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)

    payload = cb.payload or ""
    try:
        support_chat_id = int(payload.split(":", 1)[1])
    except Exception:
        return

    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    chat = await get_chat_by_id(support_chat_id)
    if not chat:
        kb = InlineKeyboard(rows=[[InlineButton("🔄 Обновить", callback_data="ag_refresh")]])
        await _send_or_edit(ctx, chat_id, "Чат не найден.", keyboard=kb, message_id=mid)
        return

    agent = _get_agent_ctx(ctx)
    if not agent:
        await handle_agent_start(ctx, cb)
        return

    st = (chat.get("status") or "").strip().lower()
    chat_spec_id = int(chat.get("specialist_id") or 0)

    # Закрыт
    if st == "closed":
        kb = InlineKeyboard(rows=[
            [await _chat_webapp_button(support_chat_id, role="agent", actor_id=agent.max_user_id)],
            [InlineButton("⬅️ К списку чатов", callback_data="ag_back_to_chats")],
        ])
        await _send_or_edit(ctx, chat_id, "🔒 Обращение уже закрыто.", keyboard=kb, message_id=mid)
        return

    # Директор
    if agent.role == "director":
        if st not in ("active", "pending", "consultation"):
            kb = InlineKeyboard(rows=[[InlineButton("⬅️ К списку", callback_data="ag_back_to_chats")]])
            await _send_or_edit(ctx, chat_id, "⚠️ Нельзя отвечать в этот чат.", keyboard=kb, message_id=mid)
            return

        prof = int(chat.get("profession") or 0)
        if prof == 2 and st == "pending" and chat_spec_id == 0:
            kb = InlineKeyboard(rows=[
                [await _chat_webapp_button(support_chat_id, role="director", actor_id=agent.max_user_id)],
                [InlineButton("✅ Принять", callback_data=f"ag_offer_yes:{support_chat_id}")],
                [InlineButton("❌ Отклонить", callback_data=f"ag_offer_no:{support_chat_id}")],
                [InlineButton("⬅️ К списку", callback_data="ag_back_to_chats")],
            ])
            await _send_or_edit(
                ctx, chat_id,
                "Заявка по «Услугам» ещё не принята.\nНажмите «Принять», чтобы начать переписку.",
                keyboard=kb, message_id=mid,
            )
            return

        await _show_chat_by_id(ctx, chat_id, support_chat_id, mid)
        return

    # Обычный специалист — pending без specialist_id → предложить принять
    if st == "pending" and chat_spec_id == 0:
        kb = InlineKeyboard(rows=[
            [InlineButton("✅ Принять", callback_data=f"ag_offer_yes:{support_chat_id}")],
            [InlineButton("❌ Отклонить", callback_data=f"ag_offer_no:{support_chat_id}")],
            [InlineButton("⬅️ К списку", callback_data="ag_back_to_chats")],
        ])
        await _send_or_edit(ctx, chat_id, "Заявка ещё не принята. Примите её, чтобы начать.", keyboard=kb, message_id=mid)
        return

    # Не мой чат
    if chat_spec_id != agent.specialist_id:
        kb = InlineKeyboard(rows=[[InlineButton("⬅️ К списку", callback_data="ag_back_to_chats")]])
        await _send_or_edit(ctx, chat_id, "⚠️ Эта заявка не закреплена за вами.", keyboard=kb, message_id=mid)
        return

    if st not in ("active", "pending", "consultation"):
        kb = InlineKeyboard(rows=[[InlineButton("⬅️ К списку", callback_data="ag_back_to_chats")]])
        await _send_or_edit(ctx, chat_id, "⚠️ Нельзя отвечать в этот чат.", keyboard=kb, message_id=mid)
        return

    await _show_chat_by_id(ctx, chat_id, support_chat_id, mid)


# ══════════════════════════════════════════════════════════════════════
# Принять заявку
# ══════════════════════════════════════════════════════════════════════

async def handle_ag_offer_accept(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)

    agent = _get_agent_ctx(ctx)
    if not agent:
        # user_data может очиститься после рестарта — восстановим контекст по cb.user
        await handle_agent_start(ctx, cb)
        return

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")

    chat = await get_chat_by_id(support_chat_id)
    if not chat:
        kb = InlineKeyboard(rows=[[InlineButton("🔄 Обновить", callback_data="ag_refresh")]])
        await ctx.api.send_message(chat_id, "⚠️ Чат не найден.", keyboard=kb)
        return

    ok = await try_accept_chat(support_chat_id, agent.specialist_id)
    if not ok:
        # Удаляем stale notice с кнопками, если заявку уже забрал другой специалист.
        stale_mid = cb.message.message_id if cb.message else None
        if stale_mid:
            result = await ctx.api.delete_message(str(stale_mid))
            if not result.get("ok"):
                if int(result.get("http_status") or 0) == 403:
                    logger.debug("skip forbidden delete stale MAX specialist notice: mid=%s", stale_mid)
                else:
                    logger.warning("failed to delete stale MAX specialist notice: mid=%s result=%s", stale_mid, result)
        kb = InlineKeyboard(rows=[[InlineButton("🔄 Обновить", callback_data="ag_refresh")]])
        await ctx.api.send_message(chat_id, "⚠️ Эту заявку уже принял другой специалист или она закрыта.", keyboard=kb)
        return

    # Удаляем офферы
    try:
        await delete_chat_offers(support_chat_id)
    except Exception:
        pass

    # Уведомляем клиента через MAX (если client_user_id — это internal DB id)
    try:
        client_db_id = int(chat.get("client_user_id") or 0)
        if client_db_id:
            from db import get_user_by_internal_id
            client_user = await get_user_by_internal_id(client_db_id)
            max_uid_client = int(client_user.get("max_user_id") or 0) if client_user else 0
            if max_uid_client:
                kb_client = InlineKeyboard(rows=[
                    [InlineButton("К заявке", callback_data=f"cc_mychat:{support_chat_id}")],
                    [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
                    [InlineButton("🔙 В меню", callback_data="back_to_main")],
                ])
                await _send_client_notice(
                    ctx.api, max_uid_client, support_chat_id,
                    "✅ Ваша заявка принята. Можете писать в чат.",
                    keyboard=kb_client,
                )
    except Exception as e:
        logger.error("notify client about accept: %s", e)

    kb = InlineKeyboard(rows=[
        [InlineButton("💬 Ответить", callback_data=f"ag_chat:{support_chat_id}")],
        [InlineButton("📂 К списку чатов", callback_data="ag_refresh")],
    ])
    await _send_or_edit(ctx, chat_id, "✅ Вы приняли заявку.", keyboard=kb)


# ══════════════════════════════════════════════════════════════════════
# Отклонить заявку
# ══════════════════════════════════════════════════════════════════════

async def handle_ag_offer_decline(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)

    agent = _get_agent_ctx(ctx)
    if not agent:
        # user_data может очиститься после рестарта — восстановим контекст по cb.user
        await handle_agent_start(ctx, cb)
        return

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")

    chat = await get_chat_by_id(support_chat_id)
    if not chat:
        kb = InlineKeyboard(rows=[[InlineButton("🔄 Обновить", callback_data="ag_refresh")]])
        await ctx.api.send_message(chat_id, "⚠️ Чат не найден.", keyboard=kb)
        return

    await mark_chat_declined(support_chat_id, agent.specialist_id)

    total_specs = await count_active_specialists_for_profession(int(chat.get("profession") or agent.profession))
    declined = await count_declines_for_chat(support_chat_id)

    if total_specs > 0 and declined >= total_specs:
        pick = await get_least_busy_specialist_for_profession(int(chat.get("profession") or agent.profession))
        if pick and (await force_assign_chat(support_chat_id, int(pick["id"]))):
            # Уведомляем назначенного спеца (если у него есть max_user_id)
            pick_max = int(pick.get("max_user_id") or 0)
            if pick_max:
                try:
                    kb_pick = InlineKeyboard(rows=[
                        [InlineButton("💬 Ответить", callback_data=f"ag_chat:{support_chat_id}")],
                    ])
                    await upsert_notice_max(
                        max_api=ctx.api,
                        max_user_id=int(pick_max),
                        kind=NoticeKind.SPECIALIST_CHAT,
                        entity_id=int(support_chat_id),
                        text="⚠️ Все специалисты отказались — заявка назначена вам автоматически.",
                        keyboard=kb_pick,
                    )
                except Exception as e:
                    logger.error("notify force-assigned: %s", e)

            # Уведомляем клиента
            try:
                client_db_id = int(chat.get("client_user_id") or 0)
                if client_db_id:
                    from db import get_user_by_internal_id
                    client_user = await get_user_by_internal_id(client_db_id)
                    max_uid_client = int(client_user.get("max_user_id") or 0) if client_user else 0
                    if max_uid_client:
                        kb_cl = InlineKeyboard(rows=[
                            [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
                            [InlineButton("🔙 В меню", callback_data="back_to_main")],
                        ])
                        await _send_client_notice(
                            ctx.api, max_uid_client, support_chat_id,
                            "✅ Ваше обращение принято. Можете писать в чат.",
                            keyboard=kb_cl,
                        )
            except Exception as e:
                logger.error("notify client force-assign: %s", e)

            kb = InlineKeyboard(rows=[[InlineButton("📂 К списку", callback_data="ag_refresh")]])
            await ctx.api.send_message(
                chat_id,
                "Вы отказались. Все отказались — заявка назначена специалисту автоматически.",
                keyboard=kb,
            )
            return

    kb = InlineKeyboard(rows=[[InlineButton("📂 К списку", callback_data="ag_refresh")]])
    await ctx.api.send_message(
        chat_id,
        "Вы отказались. Заявка остаётся в очереди — её может принять другой специалист.",
        keyboard=kb,
    )


# ══════════════════════════════════════════════════════════════════════
# Закрытие обращения (двустороннее)
# ══════════════════════════════════════════════════════════════════════

async def handle_ag_close(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Запрос на закрытие от специалиста."""
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    kb = InlineKeyboard(rows=[
        [InlineButton("✅ Да", callback_data=f"ag_close_yes:{support_chat_id}")],
        [InlineButton("❌ Нет", callback_data=f"ag_close_no:{support_chat_id}")],
    ])
    await _send_or_edit(
        ctx, chat_id,
        "Вы точно уверены, что все работы по обращению выполнены?",
        keyboard=kb, message_id=mid,
    )
    _set_state(ctx, ST_AG_CLOSE_CONFIRM)


async def handle_ag_close_yes(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    chat = await get_chat_by_id(support_chat_id)
    if not chat:
        await _send_or_edit(ctx, chat_id, "Чат не найден.", message_id=mid)
        await _render_chat_list(ctx, chat_id)
        return

    res = await request_chat_close(support_chat_id, by="agent")
    if not res.get("ok"):
        await _send_or_edit(ctx, chat_id, "Не удалось обработать запрос.", message_id=mid)
        await _render_chat_list(ctx, chat_id)
        return

    if res.get("just_closed"):
        await add_chat_message(support_chat_id, SENDER_SPEC, "✅ Обращение завершено по согласию обеих сторон")

        # уведомляем клиента на обеих платформах
        try:
            client_db_id = int(chat.get("client_user_id") or 0)
            if client_db_id:
                from db import get_user_by_internal_id
                client_user = await get_user_by_internal_id(client_db_id)
                close_msg = (
                    f"✅ Обращение «{chat.get('category') or '—'}» завершено по согласию обеих сторон.\n\n"
                    "Если нужна помощь — создайте новое обращение через меню."
                )
                max_uid_client = int(client_user.get("max_user_id") or 0) if client_user else 0
                if max_uid_client:
                    kb_cl = InlineKeyboard(rows=[
                        [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
                    ])
                    await _send_client_notice(
                        ctx.api, max_uid_client, support_chat_id, close_msg, keyboard=kb_cl,
                    )
                tg_uid_client = int(client_user.get("tg_user_id") or 0) if client_user else 0
                if tg_uid_client:
                    await notify_specialist_tg(
                        tg_uid_client, close_msg,
                        reply_markup={"inline_keyboard": [
                            [{"text": "📂 Мои обращения", "callback_data": "cc_my_chats"}],
                        ]},
                    )
        except Exception as e:
            logger.error("notify client close: %s", e)

        await _send_or_edit(ctx, chat_id, "✅ Чат закрыт (подтверждено обеими сторонами).", message_id=mid)
        await _render_chat_list(ctx, chat_id)
        return

    # Клиент ещё не подтвердил
    await add_chat_message(support_chat_id, SENDER_SPEC, "🔒 Запрашиваем завершение обращения (ожидаем клиента)")

    try:
        client_db_id = int(chat.get("client_user_id") or 0)
        if client_db_id:
            from db import get_user_by_internal_id
            client_user = await get_user_by_internal_id(client_db_id)
            close_req_msg = "🔒 Специалист предлагает завершить обращение.\nЧат закроется только если вы подтвердите."
            max_uid_client = int(client_user.get("max_user_id") or 0) if client_user else 0
            if max_uid_client:
                kb_cl = InlineKeyboard(rows=[
                    [InlineButton("✅ Подтвердить завершение", callback_data=f"cc_close:{support_chat_id}")],
                    [InlineButton("↩️ Продолжить общение", callback_data=f"cc_close_no:{support_chat_id}")],
                    [InlineButton("📂 Мои обращения", callback_data="cc_my_chats")],
                ])
                await _send_client_notice(
                    ctx.api, max_uid_client, support_chat_id, close_req_msg, keyboard=kb_cl,
                )
            tg_uid_client = int(client_user.get("tg_user_id") or 0) if client_user else 0
            if tg_uid_client:
                await notify_specialist_tg(
                    tg_uid_client, close_req_msg,
                    reply_markup={"inline_keyboard": [
                        [{"text": "✅ Подтвердить завершение", "callback_data": f"cc_close:{support_chat_id}"}],
                        [{"text": "↩️ Продолжить общение", "callback_data": f"cc_close_no:{support_chat_id}"}],
                        [{"text": "📂 Мои обращения", "callback_data": "cc_my_chats"}],
                    ]},
                )
    except Exception as e:
        logger.error("notify client close request: %s", e)

    kb = InlineKeyboard(rows=[
        [InlineButton("💬 Ответить", callback_data=f"ag_chat:{support_chat_id}")],
    ])
    await _send_or_edit(
        ctx, chat_id,
        "🔒 Запрос на завершение отправлен клиенту.\nОжидаем подтверждения.",
        keyboard=kb, message_id=mid,
    )
    _set_state(ctx, ST_AG_MENU)


async def handle_ag_close_no(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    await _show_chat_by_id(ctx, chat_id, support_chat_id, mid)
    _set_state(ctx, ST_AG_MENU)


async def handle_ag_close_cancel(ctx: MaxBotContext, cb: MaxCallback) -> None:
    """Специалист отменяет запрос на закрытие (сбрасываем флаги, продолжаем чат)."""
    if not cb.user:
        return
    await _clear_specialist_service_notices_for_callback(ctx, cb)

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    chat = await get_chat_by_id(support_chat_id)
    if not chat:
        await _send_or_edit(ctx, chat_id, "Чат не найден.", message_id=mid)
        await _render_chat_list(ctx, chat_id)
        return

    if str(chat.get("status")) == "closed":
        await _send_or_edit(ctx, chat_id, "Чат уже закрыт.", message_id=mid)
        await _render_chat_list(ctx, chat_id)
        return

    await reset_chat_close_votes(support_chat_id)
    await add_chat_message(support_chat_id, SENDER_SPEC, "↩️ Мы отменили завершение (продолжаем общение)")

    # уведомляем клиента на обеих платформах
    try:
        client_db_id = int(chat.get("client_user_id") or 0)
        if client_db_id:
            from db import get_user_by_internal_id
            client_user = await get_user_by_internal_id(client_db_id)
            cancel_msg = "↩️ Мы не подтвердили завершение. Продолжаем общение."
            max_uid_client = int(client_user.get("max_user_id") or 0) if client_user else 0
            if max_uid_client:
                kb_cl = InlineKeyboard(rows=[
                    [InlineButton("🔙 В меню", callback_data="back_to_main")],
                ])
                await _send_client_notice(
                    ctx.api, max_uid_client, support_chat_id, cancel_msg, keyboard=kb_cl,
                )
            tg_uid_client = int(client_user.get("tg_user_id") or 0) if client_user else 0
            if tg_uid_client:
                await notify_specialist_tg(
                    tg_uid_client, cancel_msg,
                    reply_markup={"inline_keyboard": [
                        [{"text": "🔙 В меню", "callback_data": "cc_menu"}],
                    ]},
                )
    except Exception as e:
        logger.error("notify client close cancel: %s", e)

    kb = InlineKeyboard(rows=[
        [InlineButton("💬 Ответить", callback_data=f"ag_chat:{support_chat_id}")],
    ])
    await _send_or_edit(ctx, chat_id, "↩️ Закрытие отменено. Продолжаем чат.",
                        keyboard=kb, message_id=mid)
    _set_state(ctx, ST_AG_MENU)


# ══════════════════════════════════════════════════════════════════════
# Переназначение (директор)
# ══════════════════════════════════════════════════════════════════════

async def handle_ag_reassign(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return

    agent = _get_agent_ctx(ctx)
    if not agent or agent.role != "director":
        return

    payload = cb.payload or ""
    support_chat_id = int(payload.split(":", 1)[1]) if ":" in payload else 0
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    chat = await get_chat_by_id(support_chat_id)
    if not chat:
        await _send_or_edit(ctx, chat_id, "Чат не найден.", message_id=mid)
        return

    specs = await list_specialists_for_profession(int(chat.get("profession") or 0))

    rows = []
    for s in specs:
        rows.append([
            InlineButton(f"👤 {s.get('name') or s['id']}", callback_data=f"ag_reassign_to:{support_chat_id}:{s['id']}")
        ])
    rows.append([InlineButton("⬅️ Назад", callback_data=f"ag_chat:{support_chat_id}")])

    await _send_or_edit(
        ctx, chat_id,
        "Выберите специалиста для переназначения:",
        keyboard=InlineKeyboard(rows=rows),
        message_id=mid,
    )


async def handle_ag_reassign_to(ctx: MaxBotContext, cb: MaxCallback) -> None:
    if not cb.user:
        return

    payload = cb.payload or ""
    parts = payload.split(":")
    # ag_reassign_to:<chat_id>:<spec_id>
    if len(parts) < 3:
        return

    support_chat_id = int(parts[1])
    spec_id = int(parts[2])
    chat_id = _resolve_chat_id(ctx, cb.message.chat_id if cb.message else "")
    mid = cb.message.message_id if cb.message else None

    ok = await force_assign_chat(support_chat_id, spec_id)
    if not ok:
        await _send_or_edit(ctx, chat_id, "Не удалось переназначить чат.", message_id=mid)
        return

    spec = await get_specialist_by_id(spec_id)

    # Уведомляем нового специалиста через MAX
    if spec:
        max_uid_spec = int(spec.get("max_user_id") or 0)
        if max_uid_spec:
            try:
                kb_spec = InlineKeyboard(rows=[
                    [InlineButton("💬 Ответить", callback_data=f"ag_chat:{support_chat_id}")],
                ])
                await upsert_notice_max(
                    max_api=ctx.api,
                    max_user_id=int(max_uid_spec),
                    kind=NoticeKind.SPECIALIST_CHAT,
                    entity_id=int(support_chat_id),
                    text="📨 Вам назначено новое обращение директором.",
                    keyboard=kb_spec,
                )
            except Exception:
                pass

    spec_name = spec.get("name") if spec else str(spec_id)
    await _send_or_edit(ctx, chat_id, f"✅ Чат переназначен: {spec_name}", message_id=mid)
    await _render_chat_list(ctx, chat_id)


# ══════════════════════════════════════════════════════════════════════
# Обработка текста по state (для max_handlers.py)
# ══════════════════════════════════════════════════════════════════════

async def handle_ag_text_by_state(ctx: MaxBotContext, msg: MaxMessage) -> bool:
    """Возвращает True если обработано."""
    if not msg.sender:
        return False

    state = ctx.user_data.get("_state", ST_IDLE)
    ag = _get_agent_ctx(ctx)

    if state == ST_AG_WAIT_PHONE:
        return await _on_agent_phone(ctx, msg)

    # Если пользователь уже в режиме специалиста, не отдаём сообщение
    # в клиентский fallback-обработчик с кнопкой "В меню".
    # Вместо этого показываем список заявок специалиста.
    if ag and state in (ST_IDLE, ST_AG_MENU):
        await _render_chat_list(ctx, msg.chat_id)
        return True

    return False


# ══════════════════════════════════════════════════════════════════════
# Фоновые задачи
# ══════════════════════════════════════════════════════════════════════

async def max_support_pending_notifier(bot: MaxBot) -> None:
    """Периодическое уведомление специалистов о новых заявках (через MAX)."""
    try:
        specs = await list_ready_specialists_for_notify_max()
    except Exception as e:
        logger.error("list_ready_specialists_for_notify_max: %s", e)
        return

    if not specs:
        return

    # Считаем pending по всем профессиям
    try:
        total_pending = await count_pending_unassigned()
    except Exception:
        total_pending = 0

    if total_pending == 0:
        return

    # bot_data для mute
    bd = bot.bot_data if hasattr(bot, "bot_data") else {}
    mute = bd.get("spec_mute_until_ts")
    if not isinstance(mute, dict):
        mute = {}
        bd["spec_mute_until_ts"] = mute

    now = time.time()

    for spec in specs:
        max_uid = int(spec.get("max_user_id") or 0)
        if not max_uid:
            continue

        # проверяем mute
        muted_until = mute.get(max_uid, 0)
        if now < muted_until:
            continue

        prof = int(spec.get("profession") or 0)
        role = spec.get("role") or "specialist"

        try:
            if role == "director":
                cnt = total_pending
            else:
                cnt = await count_pending_unassigned(profession=prof)
        except Exception:
            cnt = 0

        if cnt == 0:
            continue

        try:
            kb = InlineKeyboard(rows=[
                [InlineButton("📂 Открыть список", callback_data="ag_refresh")],
            ])
            await upsert_notice_max(
                max_api=bot.api,
                max_user_id=int(max_uid),
                kind=NoticeKind.SPECIALIST_PENDING,
                entity_id=int(max_uid),
                text=f"📬 У вас {cnt} непринятых заявок. Откройте список, чтобы обработать.",
                keyboard=kb,
            )
            # mute на 5 минут
            mute[max_uid] = now + 300
        except Exception as e:
            logger.error("notify spec %s: %s", max_uid, e)


# ══════════════════════════════════════════════════════════════════════
# Уведомление об оплате
# ══════════════════════════════════════════════════════════════════════

async def notify_agent_payment_success_max(api, payment_id: str) -> None:
    """Вызывается из webapp при подтверждении оплаты."""
    try:
        pay = await get_payment_by_payment_id(payment_id)
        if not pay:
            return
        chat = await get_chat_by_id(int(pay["chat_id"]))
        if not chat:
            return

        spec_id = int(chat.get("specialist_id") or 0)
        if not spec_id:
            return

        spec = await get_specialist_by_id(spec_id)
        if not spec:
            return

        max_uid = int(spec.get("max_user_id") or 0)
        if not max_uid:
            return

        await upsert_notice_max(
            max_api=api,
            max_user_id=int(max_uid),
            kind=NoticeKind.SPECIALIST_CHAT,
            entity_id=(zlib.crc32(str(payment_id).encode("utf-8")) & 0x7FFFFFFF),
            text=f"✅ Оплата подтверждена (payment_id={payment_id}) по чату #{chat['id']}",
        )
    except Exception as e:
        logger.error("notify_agent_payment_success_max: %s", e)


# ══════════════════════════════════════════════════════════════════════
# Регистрация обработчиков
# ══════════════════════════════════════════════════════════════════════

def register_agent_chat_handlers(bot: MaxBot) -> None:
    bot.register_command("/agent", handle_agent_start)
    bot.register_callback("ag_refresh", handle_ag_refresh)
    bot.register_callback("ag_page:*", handle_ag_page)
    bot.register_callback("ag_page_noop", handle_ag_page_noop)
    bot.register_callback("ag_ref_qr", handle_ag_ref_qr)
    bot.register_callback("ag_ref_qr:telegram", handle_ag_ref_qr)
    bot.register_callback("ag_ref_qr:max", handle_ag_ref_qr)
    bot.register_callback("ag_dir_prof:*", handle_ag_dir_prof)
    bot.register_callback("ag_dir_sections", handle_ag_dir_sections)
    bot.register_callback("ag_back_to_chats", handle_ag_back_to_chats)
    bot.register_callback("ag_chat:*", handle_ag_open_chat)
    bot.register_callback("ag_offer_yes:*", handle_ag_offer_accept)
    bot.register_callback("ag_offer_no:*", handle_ag_offer_decline)
    bot.register_callback("ag_close:*", handle_ag_close)
    bot.register_callback("ag_close_yes:*", handle_ag_close_yes)
    bot.register_callback("ag_close_no:*", handle_ag_close_no)
    bot.register_callback("ag_close_cancel:*", handle_ag_close_cancel)
    bot.register_callback("ag_reassign:*", handle_ag_reassign)
    bot.register_callback("ag_reassign_to:*", handle_ag_reassign_to)
    logger.info("MAX-бот: обработчики агентского чата подключены")
