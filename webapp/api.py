# api.py
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from datetime import datetime
import os
import uuid
import mimetypes
import httpx
import asyncio
import time
from urllib.parse import urlencode

try:
    from .utils import (
        _chat_dir, can_write, watch_payment_until_paid, finalize_chat_payment_success,
        tg_send_and_cleanup_previous, max_send_and_cleanup_previous, webapp_markup,
        build_webapp_open_chat_for_other_side, _chat_brief,
        PAYMENT_WATCH_TASKS, NOTICE_MSG_IDS, require_token_for_chat,
        _is_paid_status, _run_blocking, get_personal_data,
        get_pending_client_messages_left, PENDING_CLIENT_MSG_LIMIT,
        _get_pending_msg_lock, resolve_effective_role,
    )
    from .models import SendMessage, CreatePaymentLink, EditMessage
    from .config import BOT_TOKEN, WEBAPP_URL, FILES_DIR, MAX_FILE_SIZE
    from .asterisk_bridge import start_chat_call, is_call_in_progress
except ImportError:  # pragma: no cover - fallback for standalone запусков
    from utils import (
        _chat_dir, can_write, watch_payment_until_paid, finalize_chat_payment_success,
        tg_send_and_cleanup_previous, max_send_and_cleanup_previous, webapp_markup,
        build_webapp_open_chat_for_other_side, _chat_brief,
        PAYMENT_WATCH_TASKS, NOTICE_MSG_IDS, require_token_for_chat,
        _is_paid_status, _run_blocking, get_personal_data,
        get_pending_client_messages_left, PENDING_CLIENT_MSG_LIMIT,
        _get_pending_msg_lock, resolve_effective_role,
    )
    from models import SendMessage, CreatePaymentLink, EditMessage
    from config import BOT_TOKEN, WEBAPP_URL, FILES_DIR, MAX_FILE_SIZE
    from asterisk_bridge import start_chat_call, is_call_in_progress
import db
import tinkoff
import logging
from services.checkout_bonus import get_bonus_checkout_info, apply_bonus_discount, cancel_bonus_discount
# Добавить логгер
logger = logging.getLogger("app")
TEST_CALLS_ONLY = os.getenv("TEST_CALLS", "").strip().lower() in ("1", "true", "yes", "on")

router = APIRouter()

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _is_call_allowed_for_status(chat_status: str | None) -> bool:
    st = str(chat_status or "").strip().lower()
    if TEST_CALLS_ONLY:
        return st == "test"
    return st in ("active", "consultation")


def _normalize_call_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return f"+{digits}" if digits else ""

@router.get("/max_entry", response_class=HTMLResponse)
async def max_entry(request: Request, WebAppStartParam: str = ""):
    """
    Точка входа для мини-приложения MAX.
    Если токен передан в URL (WebAppStartParam), разрешаем его на сервере
    и сразу рендерим chat_max.html — без клиентского редиректа.
    Иначе отдаём max_entry.html с JS-fallback через bridge.
    """
    token = (WebAppStartParam or "").strip()
    if token:
        try:
            resolved = await db.resolve_chat_access_token(token)
        except Exception as e:
            logger.error(f"MAX entry token resolve error: {e}")
            resolved = None

        if resolved:
            row = await db.get_chat_by_token(resolved)
            if row:
                role = await resolve_effective_role(
                    role_from_token=row.get("role"),
                    actor_id_raw=str(row.get("actor_id") or ""),
                    chat_id=int(row["chat_id"]),
                )
                sender = 0 if str(role).lower() == "client" else 1
                logger.info(f"MAX entry: rendering chat {row['chat_id']} for role {role}")
                return templates.TemplateResponse(
                    "chat_max.html",
                    {"request": request, "chat_id": int(row["chat_id"]), "sender": int(sender), "token": resolved},
                )

    # Fallback: JS-based extraction via MAX bridge
    logger.info("MAX entry: no valid token in URL, serving JS fallback")
    return templates.TemplateResponse("max_entry.html", {"request": request})


@router.get("/favicon.ico")
async def favicon() -> Response:
    # Убираем лишние 404 в логах от автозапросов браузера.
    return Response(status_code=204)


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, t: str, platform: str = "tg"):
    """
    IMPORTANT:
    В боте есть много кнопок со ссылками /chat?t=... (токены живут ограниченно).
    Если пользователь откроет старую кнопку спустя часы/дни, токен может быть истёкшим или заменённым.
    Здесь мы "оживляем" токен (refresh/chain resolve) и делаем 302 редирект на актуальный.
    """
    t = (t or "").strip()
    if not t:
        return HTMLResponse(
            "<html><body style='font-family:system-ui;padding:16px;'>"
            "<h3>Некорректная ссылка</h3>"
            "<p>Откройте чат заново из бота.</p>"
            "</body></html>",
            status_code=404,
        )

    try:
        resolved = await db.resolve_chat_access_token(t)
    except Exception as e:
        logger.error(f"Token resolve error: {e}")
        resolved = None

    if not resolved:
        logger.warning(f"Expired/unknown token for /chat: {t[:10]}...")
        return HTMLResponse(
            "<html><body style='font-family:system-ui;padding:16px;'>"
            "<h3>Ссылка на чат устарела</h3>"
            "<p>Откройте чат заново из бота (кнопкой «Ответить»).</p>"
            "<p><b>Ссылка истекла, пожалуйста, обновите кнопку в боте: выйдите в меню и войдите обратно</b></p>"
            "</body></html>",
            status_code=404,
        )

    if resolved != t:
        logger.info(f"Token resolved for /chat: {t[:10]}... -> {resolved[:10]}...")
        try:
            old_row = await db.get_chat_by_token(t)
            new_row = await db.get_chat_by_token(resolved)
            if old_row and new_row:
                actor_raw = old_row.get("actor_id")
                role = str(new_row.get("role") or old_row.get("role") or "").strip().lower()
                actor_tg = int(actor_raw) if str(actor_raw or "").isdigit() else None

                if role == "client" and actor_tg is None:
                    actor_tg = await db._get_user_tg_id(new_row.get("client_user_id"))

                if actor_tg:
                    chat_id = int(new_row["chat_id"])
                    order_id = None
                    req = str(new_row.get("request_type") or "").strip().lower()
                    cat = str(new_row.get("category") or "")
                    if req == "order" and cat.isdigit():
                        order_id = int(cat)

                    fresh_url = f"{WEBAPP_URL.rstrip('/')}/chat?t={resolved}"
                    key = (int(actor_tg), int(chat_id))
                    text = "🔄 Кнопка чата была обновлена. Используйте новую кнопку ниже."
                    markup = webapp_markup(chat_id, fresh_url, role, order_id=order_id)
                    await tg_send_and_cleanup_previous(key, int(actor_tg), text, reply_markup=markup)
        except Exception as e:
            logger.warning(f"Failed to auto-send refreshed webapp button: {e}")

        # Сохраняем историческое поведение (редирект на свежий токен), чтобы старые
        # кнопки/ссылки продолжали "самоисцеляться". Но если в URL уже есть telegram
        # webapp-параметры, рендерим без редиректа — некоторые клиенты могут терять
        # webapp-контекст (initData/initDataUnsafe) на 302.
        has_tg_webapp_qs = any(str(k).lower().startswith("tgwebapp") for k in request.query_params.keys())
        if not has_tg_webapp_qs:
            qp = dict(request.query_params)
            qp["t"] = resolved
            # platform и другие query-параметры сохраняем.
            qs = urlencode({k: v for k, v in qp.items() if v is not None})
            return RedirectResponse(url=f"/chat?{qs}" if qs else f"/chat?t={resolved}", status_code=302)

    row = await db.get_chat_by_token(resolved)
    if not row:
        logger.warning(f"Token resolved but chat not found: {resolved[:10]}...")
        return HTMLResponse(
            "<html><body style='font-family:system-ui;padding:16px;'>"
            "<h3>Чат не найден</h3>"
            "<p>Откройте чат заново из бота.</p>"
            "</body></html>",
            status_code=404,
        )

    role = await resolve_effective_role(
        role_from_token=row.get("role"),
        actor_id_raw=str(row.get("actor_id") or ""),
        chat_id=int(row["chat_id"]),
    )
    sender = 0 if str(role).lower() == "client" else 1

    logger.info(f"Rendering chat page for chat {row['chat_id']}, role {role}, platform {platform}")
    template_name = "chat_max.html" if platform == "max" else "chat.html"

    return templates.TemplateResponse(
        template_name,
        {"request": request, "chat_id": int(row["chat_id"]), "sender": int(sender), "token": resolved},
    )

@router.get("/api/chats/{chat_id}")
async def get_chat_meta(chat_id: int, token: str):
    logger.info(f"Getting chat meta for chat {chat_id}")
    row = await require_token_for_chat(chat_id, token)
    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        logger.warning(f"Chat {chat_id} not found")
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

    role_from_token = await resolve_effective_role(
        role_from_token=row.get("role"),
        actor_id_raw=str(row.get("actor_id") or ""),
        chat_id=int(chat_id),
    )
    writable = await can_write(chat_id=int(chat_id), role_from_token=role_from_token, chat_status=chat.get("status"))

    chat = await get_personal_data(chat, role_from_token)
    print(chat)

    st_raw = str(chat.get("status") or "").strip().lower()
    pending_left = None
    if st_raw == "pending" and str(role_from_token or "").strip().lower() == "client":
        pending_left = await get_pending_client_messages_left(int(chat_id))

    logger.info(f"Chat {chat_id} metadata retrieved, writable: {writable}")

    return {
        "chat": chat,
        "can_write": bool(writable),
        "can_call": bool(
            str(role_from_token or "").strip().lower() in ("agent", "director", "client")
            and _is_call_allowed_for_status(chat.get("status"))
        ),
        "call_in_progress": is_call_in_progress(int(chat_id)),
        "role": str(role_from_token or ""),
        "pending_client_messages_left": pending_left,
        "pending_client_messages_limit": PENDING_CLIENT_MSG_LIMIT,
    }


@router.post("/api/chats/{chat_id}/call/start")
async def start_call(chat_id: int, token: str):
    try:
        logger.info("start_call: request received chat_id=%s", chat_id)
        row = await require_token_for_chat(chat_id, token)
        role_from_token = str(row.get("role") or "").strip().lower()
        logger.info(
            "start_call: token resolved chat_id=%s role=%s actor_id=%s",
            chat_id,
            role_from_token,
            row.get("actor_id"),
        )
        if role_from_token not in ("agent", "director", "client"):
            raise HTTPException(status_code=403, detail="Only specialist or client can start call")

        chat = await db.get_chat_by_id(int(chat_id))
        logger.info("start_call: chat row chat_id=%s chat=%s", chat_id, chat)
        if not chat:
            raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

        st = str(chat.get("status") or "").strip().lower()
        if not _is_call_allowed_for_status(st):
            if TEST_CALLS_ONLY:
                raise HTTPException(status_code=400, detail="Call is available only for test chats")
            raise HTTPException(status_code=400, detail="Call is available only for accepted chats")

        spec_id = int(chat.get("specialist_id") or 0)
        spec = await db.get_specialist_by_id(spec_id) if spec_id else None
        logger.info("start_call: specialist resolved chat_id=%s specialist_id=%s specialist=%s", chat_id, spec_id, spec)
        if not spec:
            raise HTTPException(status_code=400, detail="Specialist is not assigned to chat")

        spec_phone_raw = str(spec.get("phone") or "").strip()
        client_user_id = int(chat.get("client_user_id") or 0)
        if client_user_id <= 0:
            raise HTTPException(status_code=400, detail="В чате отсутствует валидный client_user_id")

        client = await db.get_user_by_internal_id(client_user_id) or {}
        client_phone_raw = str(client.get("phone") or "").strip()
        spec_phone = _normalize_call_phone(spec_phone_raw)
        client_phone = _normalize_call_phone(client_phone_raw)
        logger.info(
            "start_call: phones fetched chat_id=%s specialist_phone_raw=%s specialist_phone=%s client_user_id=%s client_phone_raw=%s client_phone=%s",
            chat_id,
            spec_phone_raw,
            spec_phone,
            client_user_id,
            client_phone_raw,
            client_phone,
        )
        if not spec_phone or not client_phone:
            raise HTTPException(status_code=400, detail="У специалиста или клиента отсутствует телефон")

        # Автодобавление участников в telephony-справочник
        logger.info("start_call: ensure client abonent chat_id=%s client_user_id=%s", chat_id, client_user_id)
        await db.ensure_abonent_for_client(
            internal_user_id=client_user_id,
            phone=client_phone,
            display_name=str(chat.get("client_name") or "Клиент"),
        )
        logger.info("start_call: ensure specialist abonent chat_id=%s specialist_id=%s", chat_id, int(spec.get("id")))
        await db.ensure_abonent_for_specialist(
            specialist_id=int(spec.get("id")),
            phone=spec_phone,
            display_name=str(spec.get("name") or "Специалист"),
        )

        if is_call_in_progress(int(chat_id)):
            raise HTTPException(status_code=409, detail="call_already_in_progress")

        await start_chat_call(
            chat_id=int(chat_id),
            specialist_phone=spec_phone,
            client_phone=client_phone,
        )
        logger.info(
            "start_call: call task started chat_id=%s specialist_phone=%s client_phone=%s",
            chat_id,
            spec_phone,
            client_phone,
        )
        return {"ok": True, "call_in_progress": True}
    except HTTPException:
        raise
    except RuntimeError as e:
        logger.exception("start_call runtime error for chat_id=%s", chat_id)
        if str(e) == "call_already_in_progress":
            raise HTTPException(status_code=409, detail="call_already_in_progress")
        raise HTTPException(status_code=500, detail=f"Не удалось запустить звонок: {e}")
    except Exception:
        logger.exception("start_call unexpected error for chat_id=%s", chat_id)
        raise HTTPException(
            status_code=500,
            detail="Не удалось подготовить звонок. Проверьте телефоны участников и telephony-таблицы.",
        )

@router.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: int, token: str, after_id: int = 0, limit: int = 50):
    logger.info(f"Getting messages for chat {chat_id} after_id {after_id}, limit {limit}")
    await require_token_for_chat(chat_id, token)
    rows = await db.get_chat_messages_after(chat_id=int(chat_id), after_id=int(after_id), limit=int(limit))
    logger.info(f"Retrieved {len(rows)} messages for chat {chat_id}")
    return {"messages": rows}

@router.put("/api/chats/{chat_id}/messages/{message_id}")
async def edit_message(chat_id: int, message_id: int, token: str, payload: EditMessage):
    """
    Редактирование сообщения.
    Права доступа:
    - Автор сообщения может редактировать свои сообщения
    - Директор может редактировать любые сообщения в чате
    """
    logger.info(f"Editing message {message_id} in chat {chat_id}")
    row = await require_token_for_chat(chat_id, token)
    role_from_token = row.get("role")
    actor_id_raw = row.get("actor_id")
    
    text = (payload.text or "").strip()
    if not text:
        logger.warning(f"Empty text for edit message {message_id}")
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    
    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        logger.warning(f"Chat {chat_id} not found when editing message")
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")
    
    # Проверяем существование сообщения
    message = await db.get_message_by_id(int(message_id))
    if not message:
        logger.warning(f"Message {message_id} not found")
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
    
    if int(message.get("chat_id") or 0) != int(chat_id):
        logger.warning(f"Message {message_id} does not belong to chat {chat_id}")
        raise HTTPException(status_code=400, detail="Message does not belong to this chat")
    
    # Определяем editor_user_id и is_director
    editor_user_id = None
    is_director = False
    
    # actor_id из токена - это internal users.id для клиента или specialist_id для специалиста
    if role_from_token == "client":
        # Клиент редактирует
        editor_user_id = int(actor_id_raw) if str(actor_id_raw or "").isdigit() else None
        if not editor_user_id:
            # Пытаемся получить из БД
            editor_user_id = await db._get_user_internal_id_by_tg(int(actor_id_raw)) if str(actor_id_raw or "").isdigit() else None
    else:
        # Специалист/директор редактирует
        # actor_id может быть specialist_id или internal user id
        specialist_id = int(actor_id_raw) if str(actor_id_raw or "").isdigit() else None
        if specialist_id:
            # Проверяем является ли директором
            spec = await db.get_specialist_by_id(specialist_id)
            if spec and str(spec.get("role") or "").lower() == "director":
                is_director = True
            # Получаем internal user id специалиста
            editor_user_id = await db.resolve_internal_user_id(internal_user_id=specialist_id)
    
    if not editor_user_id:
        logger.warning(f"Cannot determine editor user id for role {role_from_token}, actor {actor_id_raw}")
        raise HTTPException(status_code=400, detail="Cannot determine editor identity")
    
    # Вызываем функцию обновления
    updated = await db.update_chat_message(
        message_id=int(message_id),
        new_text=text,
        editor_user_id=int(editor_user_id),
        is_director=is_director,
    )
    
    if not updated:
        logger.warning(f"Edit failed for message {message_id} by user {editor_user_id}")
        raise HTTPException(status_code=403, detail="You do not have permission to edit this message")
    
    logger.info(f"Message {message_id} edited successfully by user {editor_user_id}")
    
    # Уведомляем другую сторону об обновлении (без уведомления, просто для синхронизации)
    chat = await db.get_chat_by_id(int(chat_id))
    targets = await build_webapp_open_chat_for_other_side(
        chat=chat,
        sender_role=role_from_token,
        db=db,
        webapp_base_url=WEBAPP_URL,
    )
    # Не отправляем уведомления при редактировании - только обновляем UI
    
    return {"ok": True, "edited_at": datetime.utcnow().isoformat()}

@router.post("/api/chats/{chat_id}/messages")
async def post_message(chat_id: int, token: str, payload: SendMessage):
    logger.info(f"Posting message to chat {chat_id}, sender: {payload.sender}")
    row = await require_token_for_chat(chat_id, token)
    role_from_token = await resolve_effective_role(
        role_from_token=row.get("role"),
        actor_id_raw=str(row.get("actor_id") or ""),
        chat_id=int(chat_id),
    )
    text = (payload.text or "").strip()
    if not text:
        logger.warning(f"Empty message from chat {chat_id}")
        return {"ok": True}

    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        logger.warning(f"Chat {chat_id} not found when posting message")
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

    # Lock для pending+client: предотвращает обход лимита сообщений
    # при конкурентных запросах (TOCTOU: can_write() проверяет count,
    # а add_chat_message() вставляет в отдельной транзакции).
    chat_status_raw = str(chat.get("status") or "").strip().lower()
    role_raw = str(role_from_token or "").strip().lower()
    is_pending_client = (chat_status_raw == "pending" and role_raw == "client")
    _lock = _get_pending_msg_lock(int(chat_id)) if is_pending_client else asyncio.Lock()

    async with _lock:
        if not await can_write(chat_id=int(chat_id), role_from_token=role_from_token, chat_status=chat.get("status")):
            logger.warning(f"Chat {chat_id} is not writable for role {role_from_token}")
            if is_pending_client:
                left = await get_pending_client_messages_left(int(chat_id))
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Лимит сообщений до принятия заявки исчерпан. "
                        "Ожидайте принятия специалистом."
                    ),
                    headers={
                        "X-Pending-Messages-Left": str(left),
                        "X-Pending-Messages-Limit": str(PENDING_CLIENT_MSG_LIMIT),
                    },
                )
            raise HTTPException(status_code=403, detail="Chat is not active yet")

        role_raw = str(role_from_token or "").strip().lower()
        server_sender = 0 if role_raw == "client" else 1
        await db.add_chat_message(chat_id=int(chat_id), sender=server_sender, text=text)
        logger.info(f"Message added to chat {chat_id} from sender {server_sender} (role={role_raw})")
    
    # после сохранения сообщения:
    chat = await db.get_chat_by_id(int(chat_id))
    targets = await build_webapp_open_chat_for_other_side(
        chat=chat,
        sender_role=role_from_token,
        db=db,
        webapp_base_url=WEBAPP_URL,
    )
    if targets:
        brief = _chat_brief(chat, role_from_token, message_text=text)
        notify_text = f"💬 Новое сообщение\n\n{brief}"
        tg_target = targets.get("telegram")
        if tg_target:
            try:
                to_tg = int(tg_target["user_id"])
                key = (to_tg, int(chat.get("id") or chat_id))
                logger.info(f"Sending Telegram notification to {to_tg} for chat {chat_id}")
                await tg_send_and_cleanup_previous(key, to_tg, notify_text, reply_markup=tg_target.get("reply_markup"))
            except Exception as e:
                logger.error("TG notification failed for chat %s: %s", chat_id, e)
        max_target = targets.get("max")
        if max_target:
            try:
                to_max = int(max_target["user_id"])
                key = (to_max, int(chat.get("id") or chat_id))
                logger.info(f"Sending MAX notification to {to_max} for chat {chat_id}")
                await max_send_and_cleanup_previous(key, to_max, notify_text, keyboard_buttons=max_target.get("keyboard"))
            except Exception as e:
                logger.error("MAX notification failed for chat %s: %s", chat_id, e)

    pending_left = None
    if str(chat.get("status") or "").strip().lower() == "pending" and str(role_from_token or "").strip().lower() == "client":
        pending_left = await get_pending_client_messages_left(int(chat_id))

    return {
        "ok": True,
        "pending_client_messages_left": pending_left,
        "pending_client_messages_limit": PENDING_CLIENT_MSG_LIMIT,
    }

@router.post("/api/chats/{chat_id}/files")
async def upload_chat_file(
    chat_id: int, token: str,
    sender: int = Form(...),
    text: str = Form(""),
    file: UploadFile = File(...)
):
    logger.info(f"Uploading file to chat {chat_id}, filename: {file.filename}")
    row = await require_token_for_chat(chat_id, token)
    role_from_token = await resolve_effective_role(
        role_from_token=row.get("role"),
        actor_id_raw=str(row.get("actor_id") or ""),
        chat_id=int(chat_id),
    )

    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        logger.warning(f"Chat {chat_id} not found when uploading file")
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

    data = await file.read()
    size = len(data)
    if size <= 0:
        logger.warning(f"Empty file upload attempt to chat {chat_id}")
        raise HTTPException(status_code=400, detail="Empty file")
    if size > MAX_FILE_SIZE:
        logger.warning(f"File too large for chat {chat_id}: {size} bytes")
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_FILE_SIZE} bytes)")
    
    # Lock для pending+client (аналогично post_message)
    chat_status_raw = str(chat.get("status") or "").strip().lower()
    role_raw = str(role_from_token or "").strip().lower()
    server_sender = 0 if role_raw == "client" else 1
    is_pending_client = (chat_status_raw == "pending" and role_raw == "client")
    _lock = _get_pending_msg_lock(int(chat_id)) if is_pending_client else asyncio.Lock()

    async with _lock:
        if not await can_write(chat_id=int(chat_id), role_from_token=role_from_token, chat_status=chat.get("status")):
            logger.warning(f"Chat {chat_id} not writable for file upload")
            if is_pending_client:
                left = await get_pending_client_messages_left(int(chat_id))
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Лимит сообщений до принятия заявки исчерпан. "
                        "Ожидайте принятия специалистом."
                    ),
                    headers={
                        "X-Pending-Messages-Left": str(left),
                        "X-Pending-Messages-Limit": str(PENDING_CLIENT_MSG_LIMIT),
                    },
                )
            raise HTTPException(status_code=403, detail="Chat is not active yet")

        original_name = file.filename or "file"
        stored_name = f"{uuid.uuid4().hex}__{original_name}"
        path = _chat_dir(chat_id) / stored_name
        path.write_bytes(data)
        logger.info(f"File saved to {path}, size: {size} bytes")

        mime = file.content_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"

        caption = (text or "").strip()
        if not caption:
            caption = f"📎 {original_name}"

        message_id = await db.add_chat_message(chat_id=int(chat_id), sender=server_sender, text=caption)

    file_id = await db.add_chat_file(
        chat_id=int(chat_id),
        sender=server_sender,
        original_name=original_name,
        stored_name=f"chat_{int(chat_id)}/{stored_name}",
        mime=mime,
        size_bytes=size,
        message_id=int(message_id) if message_id else None,
    )
    logger.info(f"File metadata saved with id {file_id}, message_id={message_id}")
    
    # после сохранения файла:
    chat = await db.get_chat_by_id(int(chat_id))
    targets = await build_webapp_open_chat_for_other_side(
        chat=chat,
        sender_role=role_from_token,
        db=db,
        webapp_base_url=WEBAPP_URL,
    )
    if targets:
        brief = _chat_brief(chat, role_from_token, message_text=caption)
        notify_text = f"💬 Новое сообщение\n\n{brief}"
        tg_target = targets.get("telegram")
        if tg_target:
            try:
                to_tg = int(tg_target["user_id"])
                key = (to_tg, int(chat.get("id") or chat_id))
                logger.info(f"Sending Telegram notification for file upload to {to_tg}")
                await tg_send_and_cleanup_previous(key, to_tg, notify_text, reply_markup=tg_target.get("reply_markup"))
            except Exception as e:
                logger.error("TG notification failed for chat %s (file): %s", chat_id, e)
        max_target = targets.get("max")
        if max_target:
            try:
                to_max = int(max_target["user_id"])
                key = (to_max, int(chat.get("id") or chat_id))
                logger.info(f"Sending MAX notification for file upload to {to_max}")
                await max_send_and_cleanup_previous(key, to_max, notify_text, keyboard_buttons=max_target.get("keyboard"))
            except Exception as e:
                logger.error("MAX notification failed for chat %s (file): %s", chat_id, e)
    
    pending_left = None
    if str(chat.get("status") or "").strip().lower() == "pending" and str(role_from_token or "").strip().lower() == "client":
        pending_left = await get_pending_client_messages_left(int(chat_id))

    return {
        "ok": True,
        "pending_client_messages_left": pending_left,
        "pending_client_messages_limit": PENDING_CLIENT_MSG_LIMIT,
        "file": {
            "id": file_id,
            "chat_id": int(chat_id),
            "sender": server_sender,
            "original_name": original_name,
            "mime": mime,
            "size_bytes": size,
            "download_url": f"/api/files/{file_id}/download",
        }
    }

@router.get("/api/files/{file_id}/download")
async def download_file(file_id: int, token: str):
    logger.info(f"Downloading file {file_id}")
    meta = await db.get_chat_file_by_id(int(file_id))
    if not meta:
        logger.warning(f"File {file_id} not found in database")
        raise HTTPException(status_code=404, detail="File not found")

    chat_id = int(meta["chat_id"])
    await require_token_for_chat(chat_id, token)

    stored = meta.get("stored_name")
    if not stored:
        logger.warning(f"File {file_id} has no stored_name")
        raise HTTPException(status_code=404, detail="File path missing")

    path = FILES_DIR / stored
    if not path.exists():
        logger.warning(f"File {file_id} not found on disk at {path}")
        raise HTTPException(status_code=404, detail="File not found on disk")
    
    logger.info(f"Serving file {file_id} from {path}")
    return FileResponse(
        path=path,
        filename=meta.get("original_name") or "file",
        media_type=meta.get("mime") or "application/octet-stream"
    )

@router.get("/api/chats/{chat_id}/files")
async def list_chat_files(chat_id: int, token: str, after_id: int = 0, limit: int = 50):
    logger.info(f"Listing files for chat {chat_id} after_id {after_id}")
    await require_token_for_chat(chat_id, token)
    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        logger.warning(f"Chat {chat_id} not found when listing files")
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

    rows = await db.get_chat_files_after(int(chat_id), after_id=int(after_id), limit=int(limit))
    logger.info(f"Retrieved {len(rows)} files for chat {chat_id}")

    for r in rows:
        r["download_url"] = f"/api/files/{r['id']}/download?token={token}"

    return {"files": rows}

@router.get("/api/messages/{message_id}/files")
async def list_message_files(message_id: int, token: str):
    logger.info(f"Listing files for message {message_id}")
    rows = await db.get_files_by_message_id(int(message_id))
    if not rows:
        logger.info(f"No files found for message {message_id}")
        return {"files": []}

    chat_id = int(rows[0]["chat_id"])
    await require_token_for_chat(chat_id, token)
    logger.info(f"Found {len(rows)} files for message {message_id}")

    for r in rows:
        r["download_url"] = f"/api/files/{r['id']}/download?token={token}"

    return {"files": rows}

@router.post("/api/auth/refresh")
async def refresh_token(token: str = Form(...)):
    """
    Возвращает актуальный токен для чата.
    """
    token = (token or "").strip()
    logger.info(f"Refreshing token: {token[:10]}...")
    new_token = await db.resolve_chat_access_token(token)
    if not new_token:
        logger.warning(f"Failed to refresh/resolve token: {token[:10]}...")
        raise HTTPException(status_code=401, detail="Cannot refresh")
    logger.info(f"Token refresh/resolve ok: {token[:10]}... -> {new_token[:10]}...")
    return {"token": new_token}

@router.post("/api/payments/tinkoff/notify")
async def tinkoff_payment_notify(request: Request):
    """Webhook T-Bank: подтверждение успешной оплаты (server-to-server)."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    payment_id = str((payload or {}).get("PaymentId") or (payload or {}).get("paymentId") or "").strip()
    status = str((payload or {}).get("Status") or (payload or {}).get("status") or "").strip().upper()

    if not payment_id:
        logger.warning("Tinkoff notify without payment_id: payload=%s", payload)
        return {"ok": False}

    if not tinkoff.verify_notification_token(payload or {}):
        logger.warning("Tinkoff notify token mismatch for payment_id=%s", payment_id)
        return {"ok": False, "payment_id": payment_id}

    if _is_paid_status(status):
        await finalize_chat_payment_success(payment_id=payment_id, source="webhook")
    else:
        logger.info("Tinkoff notify ignored non-paid status=%s payment_id=%s", status, payment_id)

    return {"ok": True, "payment_id": payment_id}


@router.post("/api/chats/{chat_id}/payment-link")
async def create_payment_link(chat_id: int, payload: CreatePaymentLink, token: str):
    logger.info(f"Creating payment link for chat {chat_id}, amount: {payload.amount}")
    row = await require_token_for_chat(chat_id, token)

    token_role = str(row.get("role") or "").lower().strip()
    payload_role = str(payload.role or "").lower().strip()

    if token_role not in {"agent", "director"}:
        logger.warning("Forbidden payment-link role from token: chat=%s role=%s", chat_id, token_role)
        raise HTTPException(status_code=403, detail="Only specialist/director can create payment link")

    # Совместимость с CRM: директор может присылать payload.role="agent" (старое поведение),
    # но клиент и иные роли не должны проходить.
    if payload_role not in {"agent", "director"}:
        logger.warning("Forbidden payment-link role from payload: chat=%s role=%s", chat_id, payload_role)
        raise HTTPException(status_code=403, detail="Invalid role for payment link")
    if payload_role != token_role and not (token_role == "director" and payload_role == "agent"):
        logger.warning(
            "Role mismatch in payment-link: chat=%s token_role=%s payload_role=%s",
            chat_id,
            token_role,
            payload_role,
        )
        raise HTTPException(status_code=403, detail="Role mismatch for payment link")

    amount = int(payload.amount or 0)
    if bool(payload.use_bonus):
        logger.info("use_bonus in payment-link ignored: bonus selection is handled by client payment flow")
    if amount <= 0:
        logger.warning(f"Invalid amount {amount} for chat {chat_id}")
        raise HTTPException(status_code=400, detail="Amount must be > 0")

    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        logger.warning(f"Chat {chat_id} not found for payment creation")
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

    actor_candidates: list[int] = []
    token_actor_raw = row.get("actor_id")
    if str(token_actor_raw or "").isdigit():
        actor_candidates.append(int(token_actor_raw))
    if payload.actor_id is not None and str(payload.actor_id).isdigit():
        actor_from_payload = int(payload.actor_id)
        if actor_from_payload not in actor_candidates:
            actor_candidates.append(actor_from_payload)

    spec = None
    for actor_id in actor_candidates:
        spec = await db.get_specialist_by_tg(tg_user_id=int(actor_id), tg_username="")
        if spec:
            break
        spec = await db.get_specialist_by_max(max_user_id=int(actor_id), max_username="")
        if spec:
            break

    if not spec:
        logger.warning(
            "Specialist not found for actor ids: token_actor=%s payload_actor=%s",
            token_actor_raw, payload.actor_id
        )
        raise HTTPException(status_code=403, detail="Specialist not found")

    actor_spec_id = int(spec.get("id") or 0)
    spec_role = str(spec.get("role") or "specialist")

    chat_spec_id = int(chat.get("specialist_id") or 0)
    payment_spec_id = actor_spec_id
    if chat_spec_id > 0:
        chat_spec = await db.get_specialist_by_id(chat_spec_id)
        if chat_spec:
            payment_spec_id = chat_spec_id

    st = str(chat.get("status") or "").lower()

    if st == "closed":
        logger.warning(f"Cannot create payment for closed chat {chat_id}")
        raise HTTPException(status_code=400, detail="Chat is closed")

    # В CRM ссылку на оплату должны уметь выставлять все специалисты, у которых
    # уже есть доступ к чату (доступ подтверждается валидным chat token'ом).
    # Для директора это важно в первую очередь, но правило едино для всех спецов.
    logger.info(
        "Allowing payment creation: owner_specialist_id=%s actor_specialist_id=%s role=%s chat_id=%s status=%s",
        payment_spec_id,
        actor_spec_id,
        spec_role,
        chat_id,
        st,
    )

    order_id = tinkoff.build_tinkoff_order_id("chatpay", chat_id)
    desc = f"Оплата по обращению: {chat.get('category') or chat.get('request_type') or '—'}"

    u = await db.get_user_by_internal_id(int(chat["client_user_id"])) or {}
    phone = (u.get("phone") or "").strip() or None
    email = (u.get("email") or "").strip() or None
    if not phone and not email:
        logger.warning(f"No phone/email for client {chat['client_user_id']} in chat {chat_id}")
        raise HTTPException(status_code=400, detail="Client has no phone/email for receipt")

    from tinkoff import init_payment, build_receipt_payload

    bonus_points_applied = 0
    bonus_discount_rub = 0
    cash_amount_rub = amount
    bonus_reservation_key = None
    payment_method = "CASH_ONLY"

    if cash_amount_rub <= 0:
        raise HTTPException(status_code=400, detail="Сумма к оплате деньгами должна быть больше 0")

    items_kop = [{"name": desc, "price": cash_amount_rub * 100, "qty": 1}]
    receipt = build_receipt_payload(email=email, phone=phone, items_kop=items_kop)

    logger.info(f"Initiating Tinkoff payment: order_id={order_id}, amount={cash_amount_rub}, method={payment_method}")
    try:
        notify_url = f"{WEBAPP_URL.rstrip('/')}/api/payments/tinkoff/notify" if WEBAPP_URL else None
        resp = init_payment(order_id=order_id, amount_rub=cash_amount_rub, description=desc, receipt=receipt, notification_url=notify_url)
    except Exception:
        if bonus_reservation_key:
            try:
                await cancel_bonus_discount(bonus_reservation_key)
            except Exception:
                logger.warning("Rollback bonus reservation failed for chat %s", chat_id, exc_info=True)
        raise

    payment_id = str(resp.get("PaymentId") or resp.get("paymentId") or "")
    pay_url = str(resp.get("PaymentURL") or resp.get("PaymentUrl") or resp.get("paymentURL") or "")

    if not payment_id or not pay_url:
        if bonus_reservation_key:
            try:
                await cancel_bonus_discount(bonus_reservation_key)
            except Exception:
                logger.warning("Rollback bonus reservation failed for chat %s", chat_id, exc_info=True)
        success = resp.get("Success")
        err_code = resp.get("ErrorCode") or resp.get("errorCode")
        msg = resp.get("Message") or resp.get("message")
        details = resp.get("Details") or resp.get("details")
        logger.error(f"Tinkoff payment init failed: Success={success}, ErrorCode={err_code}, Message={msg}, Details={details}")
        raise HTTPException(
            status_code=400,
            detail=f"Init payment failed. Success={success} ErrorCode={err_code} Message={msg} Details={details}"
        )

    try:
        await db.create_chat_payment(
            chat_id=int(chat_id),
            specialist_id=int(payment_spec_id),
            amount_rub=int(amount),
            payment_id=payment_id,
            pay_url=pay_url,
            status="NEW",
            bonus_points_applied=bonus_points_applied,
            bonus_discount_rub=bonus_discount_rub,
            cash_amount_rub=cash_amount_rub,
            bonus_reservation_key=bonus_reservation_key,
            payment_method=payment_method,
        )
        logger.info(f"Payment saved to DB: payment_id={payment_id}, chat_id={chat_id}")

        task_key = str(payment_id)
        old = PAYMENT_WATCH_TASKS.get(task_key)
        if old and not old.done():
            logger.info(f"Cancelling old watch task for payment {payment_id}")
            old.cancel()

        task = asyncio.create_task(
            watch_payment_until_paid(payment_id=task_key, chat_id=int(chat_id), amount=int(cash_amount_rub))
        )
        PAYMENT_WATCH_TASKS[task_key] = task

        def _log_watch_result(done_task: asyncio.Task, *, pid: str, cid: int) -> None:
            try:
                if done_task.cancelled():
                    logger.warning("Payment watch task cancelled: payment_id=%s chat_id=%s", pid, cid)
                    return
                err = done_task.exception()
                if err:
                    logger.error(
                        "Payment watch task failed: payment_id=%s chat_id=%s",
                        pid,
                        cid,
                        exc_info=(type(err), err, err.__traceback__),
                    )
                else:
                    logger.info("Payment watch task finished: payment_id=%s chat_id=%s", pid, cid)
            except Exception:
                logger.exception("Failed to inspect payment watch task state: payment_id=%s chat_id=%s", pid, cid)

        task.add_done_callback(lambda t, pid=task_key, cid=int(chat_id): _log_watch_result(t, pid=pid, cid=cid))
        logger.info(f"Started payment watch task for {payment_id}")

        if payment_method == "MIXED":
            await db.add_chat_message(
                int(chat_id),
                1,
                f"💳 Ссылка на оплату: {cash_amount_rub} ₽\n"
                f"Списано баллами: {bonus_points_applied} (≈ {bonus_discount_rub} ₽)\n{pay_url}"
            )
        else:
            await db.add_chat_message(int(chat_id), 1, f"💳 Ссылка на оплату: {amount} ₽\n{pay_url}")
        logger.info(f"Payment message added to chat {chat_id}")
    except Exception as e:
        logger.exception(
            "Post-init payment processing failed: chat_id=%s order_id=%s payment_id=%s",
            chat_id, order_id, payment_id
        )
        raise HTTPException(
            status_code=500,
            detail=f"Платёж создан в Tinkoff (payment_id={payment_id}), но локальная запись не завершилась: {e}"
        )

    return {
        "ok": True,
        "payment_id": payment_id,
        "pay_url": pay_url,
        "amount": amount,
        "cash_amount_rub": cash_amount_rub,
        "bonus_points_applied": bonus_points_applied,
        "bonus_discount_rub": bonus_discount_rub,
        "payment_method": payment_method,
    }


@router.get("/api/chats/{chat_id}/payment-bonus-preview")
async def payment_bonus_preview(chat_id: int, amount: int, token: str):
    await require_token_for_chat(chat_id, token)
    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

    total_amount = int(amount or 0)
    if total_amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be > 0")

    bonus_info = await get_bonus_checkout_info(
        int(chat["client_user_id"]),
        float(total_amount),
    )
    if not bonus_info.get("available"):
        return {
            "available": False,
            "balance_points": int(bonus_info.get("balance") or 0),
            "max_discount_rub": 0,
            "auto_bonus_discount_rub": 0,
            "cash_amount_rub": total_amount,
            "can_use_bonus": False,
        }

    usable_points = int(bonus_info.get("usable_points") or 0)
    usable_rub = int(float(bonus_info.get("usable_rub_discount") or 0))
    return {
        "available": True,
        "balance_points": int(bonus_info.get("balance") or 0),
        "max_discount_rub": usable_rub,
        "auto_bonus_discount_rub": usable_rub,
        "cash_amount_rub": max(0, total_amount - usable_rub),
        "can_use_bonus": usable_points > 0 and (total_amount - usable_rub) > 0,
        "usable_points": usable_points,
        "max_share_pct": float(bonus_info.get("max_share_pct") or 0),
    }


@router.get("/api/chats/{chat_id}/payment-current")
async def get_current_payment(chat_id: int, token: str):
    row = await require_token_for_chat(chat_id, token)
    payment = await db.get_latest_chat_payment(int(chat_id))
    if not payment:
        return {"ok": True, "payment": None}

    result = {
        "id": int(payment.get("id") or 0),
        "payment_id": str(payment.get("payment_id") or ""),
        "status": str(payment.get("status") or ""),
        "amount_rub": int(payment.get("amount_rub") or 0),
        "cash_amount_rub": int(payment.get("cash_amount_rub") or payment.get("amount_rub") or 0),
        "bonus_points_applied": int(payment.get("bonus_points_applied") or 0),
        "bonus_discount_rub": int(payment.get("bonus_discount_rub") or 0),
        "pay_url": str(payment.get("pay_url") or ""),
        "payment_method": str(payment.get("payment_method") or "CASH_ONLY"),
    }

    # Для клиента дополнительно вернём preview по авто-списанию
    if str(row.get("role") or "") == "client":
        chat = await db.get_chat_by_id(int(chat_id))
        client_internal_id = int((chat or {}).get("client_user_id") or 0)
        bonus_info = await get_bonus_checkout_info(client_internal_id, float(result["amount_rub"])) if client_internal_id else {"available": False}
        if bonus_info.get("available"):
            usable_rub = int(float(bonus_info.get("usable_rub_discount") or 0))
            result["bonus_preview"] = {
                "max_discount_rub": usable_rub,
                "cash_amount_rub": max(0, int(result["amount_rub"]) - usable_rub),
                "usable_points": int(bonus_info.get("usable_points") or 0),
            }
        else:
            result["bonus_preview"] = {"max_discount_rub": 0, "cash_amount_rub": int(result["amount_rub"]), "usable_points": 0}
    return {"ok": True, "payment": result}


@router.post("/api/chats/{chat_id}/payment/{payment_id}/apply-bonus")
async def apply_bonus_to_existing_payment(chat_id: int, payment_id: str, token: str):
    row = await require_token_for_chat(chat_id, token)
    if str(row.get("role") or "") != "client":
        raise HTTPException(status_code=403, detail="Only client can apply bonus to payment")

    current = await db.get_payment_by_payment_id(str(payment_id))
    if not current or int(current.get("chat_id") or 0) != int(chat_id):
        raise HTTPException(status_code=404, detail="Payment not found")

    if str(current.get("status") or "").upper() != "NEW":
        raise HTTPException(status_code=400, detail="Payment is not active")

    if str(current.get("payment_method") or "").upper() == "MIXED":
        return {"ok": True, "payment_id": str(current.get("payment_id") or ""), "pay_url": str(current.get("pay_url") or "")}

    chat = await db.get_chat_by_id(int(chat_id))
    if not chat:
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found")

    amount = int(current.get("amount_rub") or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid payment amount")

    bonus_info = await get_bonus_checkout_info(int(chat["client_user_id"]), float(amount))
    if not (bonus_info.get("available") and int(bonus_info.get("usable_points") or 0) > 0):
        raise HTTPException(status_code=400, detail="No bonus available for this payment")

    reserve_result = await apply_bonus_discount(
        internal_user_id=int(chat["client_user_id"]),
        order_id=int(time.time() * 1000),
        requested_points=int(bonus_info.get("usable_points") or 0),
        order_amount_rub=float(amount),
        entity_type="chat_payment",
    )
    if not reserve_result.get("ok"):
        raise HTTPException(status_code=400, detail="Failed to reserve bonus points")

    bonus_points_applied = int(reserve_result.get("points_reserved") or 0)
    bonus_discount_rub = int(float(reserve_result.get("rub_discount") or 0))
    cash_amount_rub = int(amount - bonus_discount_rub)
    reservation_key = str(reserve_result.get("reservation_key") or "")
    if cash_amount_rub <= 0:
        await cancel_bonus_discount(reservation_key)
        raise HTTPException(status_code=400, detail="Cash amount must be > 0")

    user = await db.get_user_by_internal_id(int(chat["client_user_id"])) or {}
    phone = (user.get("phone") or "").strip() or None
    email = (user.get("email") or "").strip() or None
    if not phone and not email:
        await cancel_bonus_discount(reservation_key)
        raise HTTPException(status_code=400, detail="Client has no phone/email for receipt")

    order_id = tinkoff.build_tinkoff_order_id("chatpay", chat_id)
    desc = f"Оплата по обращению: {chat.get('category') or chat.get('request_type') or '—'}"
    from tinkoff import init_payment, build_receipt_payload
    receipt = build_receipt_payload(
        email=email, phone=phone, items_kop=[{"name": desc, "price": cash_amount_rub * 100, "qty": 1}]
    )

    try:
        notify_url = f"{WEBAPP_URL.rstrip('/')}/api/payments/tinkoff/notify" if WEBAPP_URL else None
        resp = init_payment(order_id=order_id, amount_rub=cash_amount_rub, description=desc, receipt=receipt, notification_url=notify_url)
        new_payment_id = str(resp.get("PaymentId") or resp.get("paymentId") or "")
        new_pay_url = str(resp.get("PaymentURL") or resp.get("PaymentUrl") or resp.get("paymentURL") or "")
        if not new_payment_id or not new_pay_url:
            raise RuntimeError("Invalid payment init response")
    except Exception:
        await cancel_bonus_discount(reservation_key)
        raise HTTPException(status_code=400, detail="Не удалось создать mixed-платёж")

    old_task = PAYMENT_WATCH_TASKS.get(str(payment_id))
    if old_task and not old_task.done():
        old_task.cancel()

    await db.set_payment_status_by_payment_id(str(payment_id), "CANCELED")
    await db.create_chat_payment(
        chat_id=int(chat_id),
        specialist_id=int(current.get("specialist_id") or 0),
        amount_rub=int(amount),
        payment_id=new_payment_id,
        pay_url=new_pay_url,
        status="NEW",
        bonus_points_applied=bonus_points_applied,
        bonus_discount_rub=bonus_discount_rub,
        cash_amount_rub=cash_amount_rub,
        bonus_reservation_key=reservation_key,
        payment_method="MIXED",
    )
    PAYMENT_WATCH_TASKS[new_payment_id] = asyncio.create_task(
        watch_payment_until_paid(payment_id=new_payment_id, chat_id=int(chat_id), amount=int(cash_amount_rub))
    )
    await db.add_chat_message(
        int(chat_id),
        0,
        f"✅ Применены баллы: {bonus_points_applied} (≈ {bonus_discount_rub} ₽)\n"
        f"💳 Новая ссылка на оплату: {cash_amount_rub} ₽\n{new_pay_url}"
    )
    return {
        "ok": True,
        "payment_id": new_payment_id,
        "pay_url": new_pay_url,
        "cash_amount_rub": cash_amount_rub,
        "bonus_points_applied": bonus_points_applied,
        "bonus_discount_rub": bonus_discount_rub,
    }


@router.post("/api/chats/{chat_id}/payment/{payment_id}/cancel")
async def cancel_chat_payment(chat_id: int, payment_id: str, token: str):
    await require_token_for_chat(chat_id, token)

    current = await db.get_payment_by_payment_id(str(payment_id))
    if not current or int(current.get("chat_id") or 0) != int(chat_id):
        raise HTTPException(status_code=404, detail="Payment not found")

    status = str(current.get("status") or "").upper()
    if status not in {"NEW", "BONUS_RESERVED"}:
        raise HTTPException(status_code=400, detail="Payment is not cancellable")

    reservation_key = str(current.get("bonus_reservation_key") or "").strip()
    if reservation_key:
        try:
            await cancel_bonus_discount(reservation_key)
        except Exception:
            logger.warning("Bonus reservation cancel failed for payment %s", payment_id, exc_info=True)

    watch_task = PAYMENT_WATCH_TASKS.get(str(payment_id))
    if watch_task and not watch_task.done():
        watch_task.cancel()

    await db.set_payment_status_by_payment_id(str(payment_id), "CANCELED")
    await db.add_chat_message(int(chat_id), 1, "❌ Счёт отменён.")

    return {"ok": True, "payment_id": str(payment_id), "status": "CANCELED"}
