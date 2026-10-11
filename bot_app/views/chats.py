"""Site/chat CRM views extracted from the legacy views module."""

import asyncio
import json
import logging
import mimetypes
import re
import threading
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from django.db import connection
from django.db.utils import OperationalError
from django.http import FileResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.html import strip_tags
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

import db as db_async
from ..permissions import (
    director_required,
    get_current_user,
    login_required_custom,
    tech_or_director_required,
)
from ..sql_cursor import with_sql_rewrite
from ..sql_helpers import _get_table_id_column_type
from ..utils.ru_dates import format_ru_datetime

connection = with_sql_rewrite(connection)

logger = logging.getLogger(__name__)
TASK_CLIENT_ACCESS_STATUSES = (
    'awaiting_assignment',
    'in_progress',
    'postponed',
    'done',
)

# Директория для хранения файлов чатов (общая с webapp)
FILES_DIR = Path(__file__).resolve().parent.parent.parent / "files"
FILES_DIR.mkdir(parents=True, exist_ok=True)
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB
_SUPPORT_CHAT_MESSAGES_HAS_IS_READ: bool | None = None


def _find_or_link_partner_user_id(partner_specialist_id, force_relink=False):
    from .partner import _find_or_link_partner_user_id as impl

    return impl(partner_specialist_id, force_relink)


def _resolve_partner_identity(cursor, *, client_user_id=None, client_phone=None, fallback_name=''):
    from .partner import _resolve_partner_identity as impl

    return impl(
        cursor,
        client_user_id=client_user_id,
        client_phone=client_phone,
        fallback_name=fallback_name,
    )


def _ensure_scp_order_id_column():
    from .orders import _ensure_scp_order_id_column as _ensure_scp_order_id_column_impl

    return _ensure_scp_order_id_column_impl()

def _start_payment_watch_fallback(*, payment_id: str, chat_id: int, amount: int) -> None:
    """
    Fallback-поллинг оплаты для синхронного Django-потока.
    Нужен, когда create_payment_link вызывается через async_to_sync и
    asyncio.create_task в webapp API может быть отменён при закрытии временного loop.
    """
    def _runner():
        try:
            from webapp.utils import watch_payment_until_paid
            asyncio.run(
                watch_payment_until_paid(
                    payment_id=str(payment_id),
                    chat_id=int(chat_id),
                    amount=int(amount),
                )
            )
        except Exception:
            logger.exception(
                "Fallback payment watch failed: payment_id=%s chat_id=%s amount=%s",
                payment_id,
                chat_id,
                amount,
            )

    t = threading.Thread(
        target=_runner,
        name=f"chat-payment-watch-{payment_id}",
        daemon=True,
    )
    t.start()
    logger.info(
        "Started fallback payment watcher thread: payment_id=%s chat_id=%s amount=%s thread=%s",
        payment_id,
        chat_id,
        amount,
        t.name,
    )

def _support_chat_messages_has_is_read_column() -> bool:
    """Проверяет (с кэшем), есть ли колонка is_read в support_chat_messages."""
    global _SUPPORT_CHAT_MESSAGES_HAS_IS_READ
    if _SUPPORT_CHAT_MESSAGES_HAS_IS_READ is not None:
        return _SUPPORT_CHAT_MESSAGES_HAS_IS_READ
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'support_chat_messages'
              AND COLUMN_NAME = 'is_read'
            """
        )
        _SUPPORT_CHAT_MESSAGES_HAS_IS_READ = (cursor.fetchone()[0] or 0) > 0
    return _SUPPORT_CHAT_MESSAGES_HAS_IS_READ

def _ensure_support_chat_read_state_table(cursor):
    """Создаёт таблицу read-state для чатов CRM (по сотруднику), если её нет."""
    chat_id_type = _get_table_id_column_type(cursor, 'support_chats', fallback='BIGINT')
    specialist_id_type = _get_table_id_column_type(cursor, 'specialists', fallback='BIGINT')
    message_id_type = _get_table_id_column_type(cursor, 'support_chat_messages', fallback='BIGINT')

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS support_chat_read_state (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            chat_id {chat_id_type} NOT NULL,
            specialist_id {specialist_id_type} NOT NULL,
            last_read_message_id {message_id_type} NOT NULL DEFAULT 0,
            last_read_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE INDEX ux_chat_reader (chat_id, specialist_id),
            INDEX ix_reader_chat (specialist_id, chat_id),
            INDEX ix_chat_last_read (chat_id, last_read_message_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # Индекс ускоряет единый подсчёт unread по входящим сообщениям.
    cursor.execute("SHOW INDEX FROM support_chat_messages WHERE Key_name = 'ix_chat_sender_id'")
    if not cursor.fetchone():
        cursor.execute("""
            CREATE INDEX ix_chat_sender_id
            ON support_chat_messages(chat_id, sender, id)
        """)

def _mark_chat_as_read_upto(cursor, *, chat_id: int, specialist_id: int, last_seen_incoming_id: int):
    """Обновляет read-state до конкретного уже показанного входящего сообщения."""
    _ensure_support_chat_read_state_table(cursor)
    cursor.execute(
        """
        INSERT INTO support_chat_read_state (
            chat_id,
            specialist_id,
            last_read_message_id,
            last_read_at,
            updated_at
        )
        VALUES (%s, %s, %s, NOW(), NOW())
        ON DUPLICATE KEY UPDATE
            last_read_message_id = GREATEST(last_read_message_id, VALUES(last_read_message_id)),
            last_read_at = VALUES(last_read_at),
            updated_at = VALUES(updated_at)
        """,
        [int(chat_id), int(specialist_id), max(0, int(last_seen_incoming_id or 0))],
    )

def _ensure_support_chats_time_columns(cursor):
    """Добавляет accepted_at/closed_at в support_chats, если их ещё нет.

    В проде могут прилетать параллельные запросы: оба проходят SHOW COLUMNS,
    а второй падает на ALTER с Duplicate column name. Ошибку 1060 игнорируем
    как безопасную гонку и продолжаем выполнение.
    """
    cursor.execute("SHOW COLUMNS FROM support_chats LIKE 'accepted_at'")
    if not cursor.fetchone():
        try:
            cursor.execute("ALTER TABLE support_chats ADD COLUMN accepted_at TIMESTAMP NULL DEFAULT NULL AFTER updated_at")
        except OperationalError as exc:
            if exc.args and exc.args[0] != 1060:
                raise

    cursor.execute("SHOW COLUMNS FROM support_chats LIKE 'closed_at'")
    if not cursor.fetchone():
        try:
            cursor.execute("ALTER TABLE support_chats ADD COLUMN closed_at TIMESTAMP NULL DEFAULT NULL AFTER accepted_at")
        except OperationalError as exc:
            if exc.args and exc.args[0] != 1060:
                raise

def _user_can_access_chat(user, chat_id):
    """Проверка доступа к чату.

    - Директор: доступ ко всем чатам.
    - Техподдержка (profession=1): доступ ко всем чатам техподдержки (profession=1),
      чтобы корректно работать со списком /crm/chats/.
    - Остальные специалисты:
      - к назначенным на них чатам;
      - к pending-чатам без назначенного специалиста в рамках своей профессии
        (чтобы чат можно было принять через сайт).
    """
    if user.get('is_director'):
        return True

    with connection.cursor() as cursor:
        if user.get('is_partner'):
            partner_user_id = _find_or_link_partner_user_id(user['id'])
            if not partner_user_id:
                return False
            cursor.execute(
                """
                SELECT 1
                FROM support_chats
                WHERE id = %s
                  AND client_user_id = %s
                LIMIT 1
                """,
                [chat_id, int(partner_user_id)],
            )
            return cursor.fetchone() is not None

        if user.get('is_tech_support'):
            cursor.execute(
                """
                SELECT 1
                FROM support_chats
                WHERE id = %s
                  AND profession = 1
                LIMIT 1
                """,
                [chat_id],
            )
            return cursor.fetchone() is not None

        cursor.execute(
            """
            SELECT 1
            FROM support_chats
            WHERE id = %s
              AND (
                    specialist_id = %s
                    OR (
                        status = 'pending'
                        AND specialist_id IS NULL
                        AND profession = %s
                    )
              )
            LIMIT 1
            """,
            [chat_id, user['id'], int(user.get('profession') or 0)],
        )
        if cursor.fetchone() is not None:
            return True

        # Доступ через задачу: проверяем task_clients + legacy-связи в tasks/meta_json.
        task_statuses_sql = ", ".join(["%s"] * len(TASK_CLIENT_ACCESS_STATUSES))
        cursor.execute(
            f"""
            SELECT 1
            FROM support_chats sc
            WHERE sc.id = %s
              AND EXISTS (
                    SELECT 1
                    FROM tasks t
                    WHERE t.assignee_id = %s
                      AND t.status IN ({task_statuses_sql})
                      AND (
                            EXISTS (
                                SELECT 1
                                FROM task_clients tc
                                WHERE tc.task_id = t.id
                                  AND tc.client_id = sc.client_user_id
                            )
                            OR t.client_id = sc.client_user_id
                            OR (
                                t.meta_json IS NOT NULL
                                AND JSON_VALID(t.meta_json)
                                AND (
                                    JSON_SEARCH(t.meta_json, 'one', CAST(sc.client_user_id AS CHAR), NULL, '$.linked_client_ids[*]') IS NOT NULL
                                    OR JSON_SEARCH(t.meta_json, 'one', CAST(sc.client_user_id AS CHAR), NULL, '$.linked_clients[*].id') IS NOT NULL
                                    OR JSON_UNQUOTE(JSON_EXTRACT(t.meta_json, '$.client_id')) = CAST(sc.client_user_id AS CHAR)
                                )
                            )
                      )
              )
            LIMIT 1
            """,
            [chat_id, user['id'], *TASK_CLIENT_ACCESS_STATUSES],
        )
        return cursor.fetchone() is not None

def _notify_chat_participant(chat_id: int, sender_role: str, *, accepted: bool = False):
    """Best-effort: уведомить вторую сторону о новом сообщении/принятии из Django webapp."""
    try:
        import asyncio as _asyncio_local
        import db as chat_db
        from webapp.utils import (
            build_webapp_open_chat_for_other_side,
            tg_send_and_cleanup_previous,
            max_send_and_cleanup_previous,
        )
        from webapp.config import WEBAPP_URL as WEBAPP_BASE_URL

        async def _runner():
            chat = await chat_db.get_chat_by_id(int(chat_id))
            if not chat:
                return
            targets = await build_webapp_open_chat_for_other_side(
                chat=chat,
                sender_role=sender_role,
                db=chat_db,
                webapp_base_url=WEBAPP_BASE_URL,
            )
            if not targets:
                return

            brief = []
            req = (chat.get("request_type") or "").strip()
            cat = (chat.get("category") or "").strip()
            if req:
                brief.append(f"Тип: {req}")
            if cat:
                brief.append(f"Категория: {cat}")
            brief_text = "\n".join(brief)

            texts = []
            if accepted:
                texts.append("✅ Ваше обращение принято.")
            texts.append("💬 Новое сообщение")
            text = "\n\n".join([t for t in texts if t])
            if brief_text:
                text = f"{text}\n\n{brief_text}"

            tg_target = targets.get("telegram")
            if tg_target:
                try:
                    to_tg = int(tg_target["user_id"])
                    key = (to_tg, int(chat.get("id") or chat_id))
                    logger.info("Sending Telegram chat notification: chat_id=%s target=%s", chat.get("id") or chat_id, to_tg)
                    await tg_send_and_cleanup_previous(key, to_tg, text, reply_markup=tg_target.get("reply_markup"))
                except Exception:
                    logger.exception(
                        "Telegram notify failed for chat_id=%s target=%s",
                        chat.get("id") or chat_id,
                        tg_target.get("user_id"),
                    )

            max_target = targets.get("max")
            if max_target:
                try:
                    to_max = int(max_target["user_id"])
                    key = (to_max, int(chat.get("id") or chat_id))
                    logger.info("Sending MAX chat notification: chat_id=%s target=%s", chat.get("id") or chat_id, to_max)
                    await max_send_and_cleanup_previous(key, to_max, text, keyboard_buttons=max_target.get("keyboard"))
                except Exception:
                    logger.exception(
                        "MAX notify failed for chat_id=%s target=%s",
                        chat.get("id") or chat_id,
                        max_target.get("user_id"),
                    )

        _asyncio_local.run(_runner())
    except Exception:
        logger.exception("Failed to notify second side for chat %s", chat_id)

def _notify_chat_closed(chat_id: int, sender_role: str):
    """Best-effort: уведомить вторую сторону, что обращение завершено по согласию сторон."""
    try:
        import asyncio as _asyncio_local
        import db as chat_db
        from webapp.utils import (
            build_webapp_open_chat_for_other_side,
            tg_send_and_cleanup_previous,
            max_send_and_cleanup_previous,
        )
        from webapp.config import WEBAPP_URL as WEBAPP_BASE_URL

        async def _runner():
            chat = await chat_db.get_chat_by_id(int(chat_id))
            if not chat:
                return

            targets = await build_webapp_open_chat_for_other_side(
                chat=chat,
                sender_role=sender_role,
                db=chat_db,
                webapp_base_url=WEBAPP_BASE_URL,
            )
            if not targets:
                return

            category = (chat.get("category") or "—").strip() or "—"
            req = (chat.get("request_type") or "").strip().lower()
            if req == "order":
                title = f"заказа #{category}"
            else:
                title = f"обращения «{category}»"

            text = (
                f"✅ Подтверждено завершение {title}.\n\n"
                "Если нужна помощь снова — создайте новое обращение через меню."
            )

            tg_target = targets.get("telegram")
            if tg_target:
                try:
                    to_tg = int(tg_target["user_id"])
                    key = (to_tg, int(chat.get("id") or chat_id))
                    logger.info("Sending Telegram close notification: chat_id=%s target=%s", chat.get("id") or chat_id, to_tg)
                    await tg_send_and_cleanup_previous(key, to_tg, text, reply_markup=tg_target.get("reply_markup"))
                except Exception:
                    logger.exception(
                        "Telegram close notify failed for chat_id=%s target=%s",
                        chat.get("id") or chat_id,
                        tg_target.get("user_id"),
                    )

            max_target = targets.get("max")
            if max_target:
                try:
                    to_max = int(max_target["user_id"])
                    key = (to_max, int(chat.get("id") or chat_id))
                    logger.info("Sending MAX close notification: chat_id=%s target=%s", chat.get("id") or chat_id, to_max)
                    await max_send_and_cleanup_previous(key, to_max, text, keyboard_buttons=max_target.get("keyboard"))
                except Exception:
                    logger.exception(
                        "MAX close notify failed for chat_id=%s target=%s",
                        chat.get("id") or chat_id,
                        max_target.get("user_id"),
                    )

        _asyncio_local.run(_runner())
    except Exception:
        logger.exception("Failed to notify chat closure for chat %s", chat_id)

def _notify_chat_close_request(chat_id: int, sender_role: str):
    """Best-effort: уведомить вторую сторону, что инициирован запрос на завершение обращения."""
    try:
        import asyncio as _asyncio_local
        import db as chat_db
        from webapp.utils import (
            build_webapp_open_chat_for_other_side,
            tg_send_and_cleanup_previous,
            max_send_and_cleanup_previous,
        )
        from webapp.config import WEBAPP_URL as WEBAPP_BASE_URL

        async def _runner():
            chat = await chat_db.get_chat_by_id(int(chat_id))
            if not chat:
                return

            targets = await build_webapp_open_chat_for_other_side(
                chat=chat,
                sender_role=sender_role,
                db=chat_db,
                webapp_base_url=WEBAPP_BASE_URL,
            )
            if not targets:
                return

            category = (chat.get("category") or "—").strip() or "—"
            text = (
                f"🔒 Специалист предлагает завершить обращение «{category}».\n"
                "Чат закроется только после вашего подтверждения."
            )

            tg_target = targets.get("telegram")
            if tg_target:
                try:
                    to_tg = int(tg_target["user_id"])
                    key = (to_tg, int(chat.get("id") or chat_id))
                    logger.info("Sending Telegram close-request notification: chat_id=%s target=%s", chat.get("id") or chat_id, to_tg)
                    await tg_send_and_cleanup_previous(key, to_tg, text, reply_markup=tg_target.get("reply_markup"))
                except Exception:
                    logger.exception(
                        "Telegram close-request notify failed for chat_id=%s target=%s",
                        chat.get("id") or chat_id,
                        tg_target.get("user_id"),
                    )

            max_target = targets.get("max")
            if max_target:
                try:
                    to_max = int(max_target["user_id"])
                    key = (to_max, int(chat.get("id") or chat_id))
                    logger.info("Sending MAX close-request notification: chat_id=%s target=%s", chat.get("id") or chat_id, to_max)
                    await max_send_and_cleanup_previous(key, to_max, text, keyboard_buttons=max_target.get("keyboard"))
                except Exception:
                    logger.exception(
                        "MAX close-request notify failed for chat_id=%s target=%s",
                        chat.get("id") or chat_id,
                        max_target.get("user_id"),
                    )

        _asyncio_local.run(_runner())
    except Exception:
        logger.exception("Failed to notify chat close-request for chat %s", chat_id)

def _build_chat_page_payload(chat_id):
    """Подготовить данные чата для рендера страницы диалога."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT sc.id, sc.client_user_id, sc.status, sc.profession, sc.category, sc.source, sc.created_at,
                   sc.request_type,
                   u.first_name, u.last_name, u.phone, u.tg_username, u.inn,
                   s.name AS specialist_name,
                   sc.close_req_client, sc.close_req_agent
            FROM support_chats sc
            LEFT JOIN users u ON u.id = sc.client_user_id
            LEFT JOIN specialists s ON s.id = sc.specialist_id
            WHERE sc.id = %s
        """, [chat_id])
        row = cursor.fetchone()

        if not row:
            return None

        chat = {
            'id': row[0], 'client_user_id': row[1], 'status': row[2],
            'profession': row[3], 'category': row[4], 'source': row[5], 'created_at': row[6],
            'request_type': row[7],
            'client_first_name': row[8], 'client_last_name': row[9],
            'client_phone': row[10], 'client_tg': row[11], 'client_inn': row[12],
            'specialist_name': row[13],
            'close_req_client': int(row[14] or 0), 'close_req_agent': int(row[15] or 0),
        }
        chat['is_partner_director'] = (str(chat.get('request_type') or '').lower() == 'partner_director')
        client_name = f"{(chat.get('client_first_name') or '').strip()} {(chat.get('client_last_name') or '').strip()}".strip()
        if chat['is_partner_director']:
            partner_meta = _resolve_partner_identity(
                cursor,
                client_user_id=chat.get('client_user_id'),
                client_phone=chat.get('client_phone'),
                fallback_name=client_name,
            )
            chat.update(partner_meta)
            chat['header_title'] = f"Чат с партнёром: {chat.get('partner_name') or 'Партнёр'}"
        else:
            chat['header_title'] = f"Чат #{chat['id']}"
            chat['partner_name'] = ''

        source_map = {
            'telegram': 'Telegram',
            'max': 'MAX',
            'site': 'Сайт',
            'crm': 'CRM',
        }
        source_raw = str(chat.get('source') or '').strip().lower()
        chat['source_label'] = source_map.get(source_raw, (chat.get('source') or 'CRM'))

        # Сообщения чата с файлами
        cursor.execute("""
            SELECT m.id, m.sender, m.text, m.created_at,
                   f.id as file_id, f.original_name, f.mime, f.size_bytes
            FROM support_chat_messages m
            LEFT JOIN support_chat_files f ON f.message_id = m.id
            WHERE m.chat_id = %s
            ORDER BY m.created_at ASC
        """, [chat_id])
        messages = []
        messages_json = []
        last_seen_incoming_id = 0
        for r in cursor.fetchall():
            sender_val = r[1]
            if sender_val == 0 and r[0]:
                last_seen_incoming_id = max(last_seen_incoming_id, int(r[0]))
            msg = {
                'id': r[0], 'sender': 'client' if sender_val == 0 else 'specialist',
                'text': r[2], 'created_at': r[3],
                'file_id': r[4], 'file_name': r[5], 'file_mime': r[6], 'file_size': r[7],
            }
            if msg['file_id']:
                msg['file_url'] = f"/api/chat/file/{msg['file_id']}/download/"
                msg['is_image'] = (msg['file_mime'] or '').startswith('image/')
                msg['is_video'] = (msg['file_mime'] or '').startswith('video/')
                # Скрыть auto-caption "📎 filename"
                auto_caption = f"\U0001f4ce {msg['file_name']}"
                msg['has_caption'] = msg['text'] and msg['text'] != auto_caption
            messages.append(msg)
            messages_json.append({
                'sender': msg['sender'],
                'sender_code': int(sender_val),
                'text': msg.get('text') or '',
                'time': msg['created_at'].strftime('%H:%M') if msg.get('created_at') else '',
                'time_title': format_ru_datetime(msg.get('created_at'), fallback='') if msg.get('created_at') else '',
                'created_at_display': format_ru_datetime(msg.get('created_at'), fallback='') if msg.get('created_at') else '',
                'file_id': msg.get('file_id'),
                'file_name': msg.get('file_name') or '',
                'file_mime': msg.get('file_mime') or '',
                'file_size': msg.get('file_size') or 0,
                'file_url': msg.get('file_url') or '',
                'is_image': bool(msg.get('is_image')),
                'is_video': bool(msg.get('is_video')),
                'has_caption': bool(msg.get('has_caption')),
            })

    return {
        'chat': chat,
        'messages': messages,
        'messages_json': json.dumps(messages_json, ensure_ascii=False),
        'last_seen_incoming_id': last_seen_incoming_id,
    }

@ensure_csrf_cookie
@login_required_custom
def specialist_chat(request, chat_id):
    """Страница чата для специалиста"""
    user = get_current_user(request)
    if user and user.get('is_director'):
        return redirect('bot_app:crm_chat_detail', chat_id=chat_id)

    if not _user_can_access_chat(user, chat_id):
        return redirect('bot_app:specialist_panel')

    payload = _build_chat_page_payload(chat_id)
    if not payload:
        return redirect('bot_app:specialist_panel')
    with connection.cursor() as cursor:
        _mark_chat_as_read_upto(
            cursor,
            chat_id=int(chat_id),
            specialist_id=int(user['id']),
            last_seen_incoming_id=int(payload.get('last_seen_incoming_id') or 0),
        )

    requested_back_url = (request.GET.get('next') or '').strip()
    back_url = requested_back_url if requested_back_url and url_has_allowed_host_and_scheme(
        url=requested_back_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ) else None
    if not back_url:
        if user.get('is_partner'):
            back_url = '/partner/chats/'
        else:
            back_url = '/crm/chats/' if user.get('is_director') else '/specialist/'

    return render(request, 'bot_app/specialist_chat.html', {
        'user': user,
        'chat': payload['chat'],
        'messages': payload['messages'],
        'messages_json': payload['messages_json'],
        'chat_ui_mode': 'partner' if user.get('is_partner') else 'specialist',
        'back_url': back_url,
    })

@ensure_csrf_cookie
@director_required
def crm_chat_detail(request, chat_id):
    """Страница чата для директора (внутри CRM-контекста)."""
    user = get_current_user(request)
    if not _user_can_access_chat(user, chat_id):
        return redirect('bot_app:crm_chats')

    payload = _build_chat_page_payload(chat_id)
    if not payload:
        return redirect('bot_app:crm_chats')
    with connection.cursor() as cursor:
        _mark_chat_as_read_upto(
            cursor,
            chat_id=int(chat_id),
            specialist_id=int(user['id']),
            last_seen_incoming_id=int(payload.get('last_seen_incoming_id') or 0),
        )

    requested_back_url = (request.GET.get('next') or '').strip()
    back_url = requested_back_url if requested_back_url and url_has_allowed_host_and_scheme(
        url=requested_back_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ) else '/crm/chats/'

    # Ищем связанные заказы через support_chat_payments → orders
    chat_orders = []
    try:
        _ensure_scp_order_id_column()
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT o.id, o.amount, o.status, o.created_at, o.paid_at,
                       scp.payment_id, scp.order_id AS scp_order_id
                FROM support_chat_payments scp
                JOIN orders o ON (
                    (scp.order_id IS NOT NULL AND scp.order_id = o.id)
                    OR (scp.order_id IS NULL AND scp.payment_id = o.payment_id
                        AND o.payment_id IS NOT NULL AND o.payment_id != '')
                )
                WHERE scp.chat_id = %s
                ORDER BY o.created_at DESC
            """, [chat_id])
            cols_o = [col[0] for col in cursor.description]
            chat_orders = [dict(zip(cols_o, r)) for r in cursor.fetchall()]
    except Exception:
        pass

    return render(request, 'bot_app/specialist_chat.html', {
        'user': user,
        'chat': payload['chat'],
        'messages': payload['messages'],
        'messages_json': payload['messages_json'],
        'chat_ui_mode': 'director',
        'back_url': back_url,
        'chat_orders': chat_orders,
    })

@require_POST
@login_required_custom
def api_chat_create_payment(request, chat_id):
    """API: создать ссылку на оплату из CRM-чата специалиста."""
    user = get_current_user(request)
    if not _user_can_access_chat(user, chat_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому чату'}, status=403)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Некорректный JSON'}, status=400)

    amount = int(data.get('amount') or 0)
    if amount <= 0:
        return JsonResponse({'success': False, 'error': 'Сумма должна быть больше 0'}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tg_user_id, max_user_id
            FROM specialists
            WHERE id = %s
            LIMIT 1
            """,
            [user['id']],
        )
        row = cursor.fetchone()
    actor_id = None
    if row:
        actor_id = row[0] or row[1]
    actor_id = int(actor_id) if str(actor_id or "").isdigit() else None

    token_role = "director" if user.get("is_director") else "agent"

    try:
        token = async_to_sync(db_async.create_chat_access_token)(
            int(chat_id),
            token_role,
            str(actor_id) if actor_id is not None else None,
        )

        from webapp.models import CreatePaymentLink
        from webapp.api import create_payment_link

        result = async_to_sync(create_payment_link)(
            int(chat_id),
            CreatePaymentLink(
                actor_id=actor_id,
                role=token_role,
                amount=int(amount),
                use_bonus=False,
            ),
            token,
        )
    except Exception as e:
        logger.exception("CRM chat payment creation failed for chat_id=%s specialist_id=%s", chat_id, user['id'])
        err_text = getattr(e, "detail", None) or str(e) or "Не удалось создать ссылку на оплату"
        return JsonResponse({'success': False, 'error': err_text}, status=400)

    payment_id = str(result.get('payment_id') or '').strip()
    cash_amount_rub = int(result.get('cash_amount_rub') or amount)
    if payment_id and cash_amount_rub > 0:
        try:
            _start_payment_watch_fallback(
                payment_id=payment_id,
                chat_id=int(chat_id),
                amount=cash_amount_rub,
            )
        except Exception:
            logger.exception(
                "Failed to start fallback payment watcher for payment_id=%s chat_id=%s",
                payment_id,
                chat_id,
            )
    else:
        logger.warning(
            "Payment watcher fallback skipped due to invalid payload: payment_id=%s chat_id=%s cash_amount_rub=%s",
            payment_id,
            chat_id,
            cash_amount_rub,
        )

    return JsonResponse({
        'success': True,
        'payment_id': payment_id,
        'pay_url': result.get('pay_url'),
    })

@require_POST
@login_required_custom
def api_send_message(request, chat_id):
    """API: отправить сообщение от специалиста/директора/партнёра."""
    user = get_current_user(request)
    if not _user_can_access_chat(user, chat_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому чату'})

    try:
        data = json.loads(request.body)
        text = data.get('text', '').strip()

        if not text:
            return JsonResponse({'success': False, 'error': 'Пустое сообщение'})

        sender_is_partner_client = bool(user.get('is_partner'))
        sender_value = 0 if sender_is_partner_client else 1

        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM support_chats WHERE id = %s", [chat_id])
            prev_row = cursor.fetchone()
            prev_status = (prev_row[0] if prev_row else '') or ''

            # Добавляем сообщение (sender: 0 = client, 1 = specialist)
            if _support_chat_messages_has_is_read_column():
                cursor.execute("""
                    INSERT INTO support_chat_messages (chat_id, sender, text, is_read, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                """, [chat_id, sender_value, text, 1 if sender_value != 0 else 0])
            else:
                cursor.execute("""
                    INSERT INTO support_chat_messages (chat_id, sender, text, created_at)
                    VALUES (%s, %s, %s, NOW())
                """, [chat_id, sender_value, text])

            if sender_is_partner_client:
                cursor.execute("""
                    UPDATE support_chats
                    SET status = CASE WHEN status = 'closed' THEN status ELSE 'active' END,
                        updated_at = NOW()
                    WHERE id = %s
                """, [chat_id])
            else:
                # Обновляем статус чата на active + фиксируем время принятия.
                # Если чат ещё не назначен (pending), закрепляем за текущим специалистом.
                cursor.execute("""
                    UPDATE support_chats
                    SET status = 'active',
                        specialist_id = COALESCE(specialist_id, %s),
                        accepted_at = COALESCE(accepted_at, NOW())
                    WHERE id = %s
                """, [user['id'], chat_id])

        _notify_chat_participant(
            chat_id,
            "client" if sender_is_partner_client else "agent",
            accepted=(str(prev_status).lower() == "pending" and not sender_is_partner_client),
        )

        return JsonResponse({'success': True, 'message': 'Сообщение отправлено'})

    except Exception as e:
        logger.exception("api_send_message failed for chat_id=%s specialist_id=%s", chat_id, user.get('id'))
        return JsonResponse({'success': False, 'error': str(e)})

@require_POST
@login_required_custom
def api_chat_upload_file(request, chat_id):
    """API: загрузить файл в чат от специалиста/директора/партнёра."""
    user = get_current_user(request)
    if not _user_can_access_chat(user, chat_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому чату'})

    try:
        uploaded = request.FILES.get('file')
        if not uploaded:
            return JsonResponse({'success': False, 'error': 'Файл не выбран'})

        if uploaded.size > MAX_FILE_SIZE:
            return JsonResponse({'success': False, 'error': f'Файл слишком большой (макс {MAX_FILE_SIZE // (1024*1024)} МБ)'})

        caption = request.POST.get('text', '').strip()
        original_name = uploaded.name or 'file'
        stored_name = f"{uuid.uuid4().hex}__{original_name}"
        mime = uploaded.content_type or mimetypes.guess_type(original_name)[0] or 'application/octet-stream'

        # Сохраняем файл на диск
        chat_dir = FILES_DIR / f"chat_{chat_id}"
        chat_dir.mkdir(parents=True, exist_ok=True)
        file_path = chat_dir / stored_name
        with open(file_path, 'wb') as f:
            for chunk in uploaded.chunks():
                f.write(chunk)

        # Текст сообщения
        if not caption:
            caption = f"\U0001f4ce {original_name}"

        sender_is_partner_client = bool(user.get('is_partner'))
        sender_value = 0 if sender_is_partner_client else 1

        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM support_chats WHERE id = %s", [chat_id])
            prev_row = cursor.fetchone()
            prev_status = (prev_row[0] if prev_row else '') or ''

            # Добавляем сообщение
            if _support_chat_messages_has_is_read_column():
                cursor.execute("""
                    INSERT INTO support_chat_messages (chat_id, sender, text, is_read, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                """, [chat_id, sender_value, caption, 1 if sender_value != 0 else 0])
            else:
                cursor.execute("""
                    INSERT INTO support_chat_messages (chat_id, sender, text, created_at)
                    VALUES (%s, %s, %s, NOW())
                """, [chat_id, sender_value, caption])
            message_id = cursor.lastrowid

            # Добавляем запись файла
            cursor.execute("""
                INSERT INTO support_chat_files (chat_id, message_id, sender, original_name, stored_name, mime, size_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, [chat_id, message_id, sender_value, original_name, f"chat_{chat_id}/{stored_name}", mime, uploaded.size])
            file_id = cursor.lastrowid

            if sender_is_partner_client:
                cursor.execute("""
                    UPDATE support_chats
                    SET status = CASE WHEN status = 'closed' THEN status ELSE 'active' END,
                        updated_at = NOW()
                    WHERE id = %s
                """, [chat_id])
            else:
                # Обновляем статус чата + фиксируем время принятия.
                # Если чат ещё не назначен (pending), закрепляем за текущим специалистом.
                cursor.execute("""
                    UPDATE support_chats
                    SET status = 'active',
                        specialist_id = COALESCE(specialist_id, %s),
                        accepted_at = COALESCE(accepted_at, NOW())
                    WHERE id = %s
                """, [user['id'], chat_id])

        _notify_chat_participant(
            chat_id,
            "client" if sender_is_partner_client else "agent",
            accepted=(str(prev_status).lower() == "pending" and not sender_is_partner_client),
        )

        return JsonResponse({
            'success': True,
            'message': 'Файл отправлен',
            'file': {
                'id': file_id,
                'original_name': original_name,
                'mime': mime,
                'size_bytes': uploaded.size,
            }
        })

    except Exception as e:
        logger.exception("api_chat_upload_file failed for chat_id=%s specialist_id=%s", chat_id, user.get('id'))
        return JsonResponse({'success': False, 'error': str(e)})

@require_POST
@login_required_custom
def api_chat_close_by_agent(request, chat_id):
    """API: специалист или директор запрашивает/подтверждает завершение обращения со стороны агента."""
    user = get_current_user(request)
    if not _user_can_access_chat(user, chat_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа к этому чату'}, status=403)

    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, status, request_type, close_req_client, close_req_agent
                FROM support_chats
                WHERE id = %s AND (request_type IS NULL OR request_type != 'order')
                LIMIT 1
            """, [chat_id])
            row = cursor.fetchone()

            if not row:
                return JsonResponse({'success': False, 'error': 'Обращение не найдено'}, status=404)

            status = row[1]
            if (status or '').lower() == 'closed':
                return JsonResponse({'success': True, 'already_closed': True, 'message': 'Обращение уже закрыто'})
            if str(row[2] or '').strip().lower() == 'partner_director':
                return JsonResponse({'success': False, 'error': 'Постоянный чат с директором нельзя закрыть'}, status=400)

            close_req_client = int(row[3] or 0)

            # Ставим флаг согласия со стороны агента
            cursor.execute("""
                UPDATE support_chats
                SET close_req_agent = 1,
                    close_req_agent_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
            """, [chat_id])

            just_closed = False
            if close_req_client == 1:
                # Клиент уже запросил — закрываем
                cursor.execute("""
                    UPDATE support_chats
                    SET status = 'closed',
                        closed_at = COALESCE(closed_at, NOW()),
                        updated_at = NOW()
                    WHERE id = %s
                """, [chat_id])
                just_closed = True

        if just_closed:
            _notify_chat_closed(chat_id, "agent")
        else:
            _notify_chat_close_request(chat_id, "agent")

        if just_closed:
            return JsonResponse({'success': True, 'just_closed': True, 'message': 'Обращение завершено'})
        else:
            return JsonResponse({'success': True, 'just_closed': False, 'message': 'Запрос на завершение отправлен. Ожидаем подтверждения клиента.'})

    except Exception as e:
        logger.exception("api_chat_close_by_agent failed for chat_id=%s specialist_id=%s", chat_id, user.get('id'))
        return JsonResponse({'success': False, 'error': str(e)})

@require_POST
@login_required_custom
@director_required
def api_crm_chat_force_close(request, chat_id):
    """API: досрочно закрыть обращение из CRM (только директор)."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, status, request_type
                FROM support_chats
                WHERE id = %s
                  AND (request_type IS NULL OR request_type != 'order')
                LIMIT 1
            """, [chat_id])
            row = cursor.fetchone()

            if not row:
                return JsonResponse({'success': False, 'error': 'Обращение не найдено'}, status=404)

            if (row[1] or '').lower() == 'closed':
                return JsonResponse({'success': True, 'message': 'Обращение уже закрыто'})
            if str(row[2] or '').strip().lower() == 'partner_director':
                return JsonResponse({'success': False, 'error': 'Постоянный чат с директором нельзя закрыть'}, status=400)

            cursor.execute("""
                UPDATE support_chats
                SET status = 'closed',
                    closed_at = COALESCE(closed_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
            """, [chat_id])

        return JsonResponse({'success': True, 'message': 'Обращение закрыто'})
    except Exception as e:
        logger.exception("api_crm_chat_force_close failed for chat_id=%s", chat_id)
        return JsonResponse({'success': False, 'error': str(e)})

@require_POST
@login_required_custom
@director_required
def api_crm_chat_transfer(request, chat_id):
    """API: передать чат другому специалисту (только директор)."""
    try:
        data = json.loads(request.body)
    except Exception:
        logger.exception("api_crm_chat_transfer: invalid JSON for chat_id=%s", chat_id)
        return JsonResponse({'success': False, 'error': 'Некорректный JSON'}, status=400)

    try:
        new_specialist_id = int(data.get('specialist_id') or 0)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Некорректный специалист'}, status=400)

    if new_specialist_id <= 0:
        return JsonResponse({'success': False, 'error': 'Выберите специалиста'}, status=400)

    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, specialist_id, status, profession
                FROM support_chats
                WHERE id = %s
                  AND (request_type IS NULL OR request_type != 'order')
                LIMIT 1
            """, [chat_id])
            chat_row = cursor.fetchone()
            if not chat_row:
                return JsonResponse({'success': False, 'error': 'Обращение не найдено'}, status=404)

            old_specialist_id = int(chat_row[1] or 0)

            if old_specialist_id == new_specialist_id:
                return JsonResponse({'success': False, 'error': 'Этот специалист уже назначен'}, status=400)

            # Нельзя назначить неактивного специалиста или директора.
            cursor.execute("""
                SELECT id, name, profession, is_director, is_active
                FROM specialists
                WHERE id = %s
                LIMIT 1
            """, [new_specialist_id])
            spec_row = cursor.fetchone()
            if not spec_row:
                return JsonResponse({'success': False, 'error': 'Специалист не найден'}, status=404)

            if int(spec_row[4] or 0) != 1:
                return JsonResponse({'success': False, 'error': 'Специалист неактивен'}, status=400)

            if int(spec_row[3] or 0) == 1:
                return JsonResponse({'success': False, 'error': 'Нельзя назначить директора'}, status=400)

            cursor.execute("""
                UPDATE support_chats
                SET specialist_id = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, [new_specialist_id, chat_id])

        return JsonResponse({
            'success': True,
            'message': 'Чат передан новому специалисту',
            'chat_id': int(chat_id),
            'old_specialist_id': old_specialist_id,
            'new_specialist_id': new_specialist_id,
            'new_specialist_name': spec_row[1],
        })
    except Exception as e:
        logger.exception("api_crm_chat_transfer failed for chat_id=%s new_specialist_id=%s", chat_id, data.get('specialist_id'))
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required_custom
def api_chat_download_file(request, file_id):
    """Скачать файл из чата"""
    user = get_current_user(request)

    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, chat_id, original_name, stored_name, mime
                FROM support_chat_files
                WHERE id = %s
            """, [file_id])
            row = cursor.fetchone()

        if not row:
            return JsonResponse({'success': False, 'error': 'Файл не найден'}, status=404)

        file_chat_id = row[1]
        if not _user_can_access_chat(user, file_chat_id):
            return JsonResponse({'success': False, 'error': 'Нет доступа к этому файлу'}, status=403)

        stored_name = row[3]
        file_path = FILES_DIR / stored_name
        if not file_path.exists():
            return JsonResponse({'success': False, 'error': 'Файл не найден на диске'}, status=404)

        return FileResponse(
            open(file_path, 'rb'),
            filename=row[2],
            content_type=row[4] or 'application/octet-stream',
        )

    except Exception as e:
        logger.exception("api_chat_download_file failed for file_id=%s user_id=%s", file_id, user.get('id') if user else None)
        return JsonResponse({'success': False, 'error': str(e)})

@login_required_custom
def api_chat_messages(request, chat_id):
    """Получить сообщения чата (с информацией о файлах)"""
    user = get_current_user(request)
    if not _user_can_access_chat(user, chat_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа', 'messages': []})
    try:
        with connection.cursor() as cursor:
            _ensure_support_chat_read_state_table(cursor)
            cursor.execute("""
                SELECT m.id, m.chat_id, m.created_at, m.sender, m.text,
                       f.id as file_id, f.original_name, f.mime, f.size_bytes
                FROM support_chat_messages m
                LEFT JOIN support_chat_files f ON f.message_id = m.id
                WHERE m.chat_id = %s
                ORDER BY m.created_at ASC
            """, [chat_id])
            columns = ['id', 'chat_id', 'created_at', 'sender', 'text',
                        'file_id', 'file_name', 'file_mime', 'file_size']
            messages = []
            last_seen_incoming_id = 0
            for row in cursor.fetchall():
                msg = dict(zip(columns, row))
                if int(msg.get('sender') or 0) == 0 and msg.get('id'):
                    last_seen_incoming_id = max(last_seen_incoming_id, int(msg['id']))
                msg['created_at_display'] = format_ru_datetime(msg.get('created_at'), fallback='') if msg.get('created_at') else ''
                if msg['created_at']:
                    msg['created_at'] = msg['created_at'].isoformat() if hasattr(msg['created_at'], 'isoformat') else msg['created_at']
                if msg['file_id']:
                    msg['file_url'] = f"/api/chat/file/{msg['file_id']}/download/"
                messages.append(msg)
            _mark_chat_as_read_upto(
                cursor,
                chat_id=int(chat_id),
                specialist_id=int(user['id']),
                last_seen_incoming_id=last_seen_incoming_id,
            )
        return JsonResponse({'success': True, 'messages': messages})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e), 'messages': []})

@login_required_custom
@tech_or_director_required
def crm_chats_list(request):
    """Список чатов поддержки.
    Доступен директору и техподдержке.
    """
    user = get_current_user(request)
    is_director = user.get('is_director', False)
    is_tech_support = user.get('is_tech_support', False)

    status_filter = request.GET.get('status', '')
    profession_filter = request.GET.get('profession', '')
    unread_filter = request.GET.get('unread', '')
    client_filter = request.GET.get('client_id', '')
    search_query = request.GET.get('q', '').strip()

    conditions = []
    params = []
    scope_conditions = []
    scope_params = []

    # Scope доступа:
    # - директор видит все чаты;
    # - техподдержка видит все чаты своего раздела (profession=1),
    #   включая назначенные на других специалистов.
    if not is_director and is_tech_support:
        scope_conditions.append("sc.profession = %s")
        scope_params.append(1)
        conditions.append("sc.profession = %s")
        params.append(1)

    if status_filter:
        conditions.append("sc.status = %s")
        params.append(status_filter)

    if profession_filter:
        conditions.append("sc.profession = %s")
        params.append(int(profession_filter))
    if client_filter:
        try:
            conditions.append("sc.client_user_id = %s")
            params.append(int(client_filter))
        except (TypeError, ValueError):
            client_filter = ''

    if search_query:
        search_conditions = [
            "u.first_name LIKE %s",
            "u.last_name LIKE %s",
            "CONCAT_WS(' ', u.first_name, u.last_name) LIKE %s",
            "CONCAT_WS(' ', u.last_name, u.first_name) LIKE %s",
            "u.phone LIKE %s",
            "u.inn LIKE %s",
            "u.tg_username LIKE %s",
        ]
        search_params = [f"%{search_query}%"] * len(search_conditions)
        if search_query.isdigit():
            search_conditions.insert(0, "sc.id = %s")
            search_params.insert(0, int(search_query))

        search_digits = re.sub(r"\D+", "", search_query)
        if search_digits:
            normalized_phone_expr = (
                "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
                "COALESCE(u.phone, ''), '+', ''), ' ', ''), '-', ''), '(', ''), ')', ''), '.', '')"
            )
            search_conditions.append(f"{normalized_phone_expr} LIKE %s")
            search_params.append(f"%{search_digits}%")

        conditions.append("(" + " OR ".join(search_conditions) + ")")
        params.extend(search_params)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    with connection.cursor() as cursor:
        _ensure_support_chats_time_columns(cursor)
        _ensure_support_chat_read_state_table(cursor)
        viewer_id = int(user['id'])

        # Фильтр непрочитанных: только входящие (sender=0), не просмотренные текущим сотрудником.
        if unread_filter:
            where += (" AND " if where else "WHERE ") + """EXISTS (
                SELECT 1 FROM support_chat_messages m
                LEFT JOIN support_chat_read_state rs
                       ON rs.chat_id = sc.id AND rs.specialist_id = %s
                WHERE m.chat_id = sc.id
                  AND m.sender = 0
                  AND m.id > COALESCE(rs.last_read_message_id, 0)
            )"""
            params.append(viewer_id)

        # Счётчики по текущему scope (для директора — полный реестр).
        _count_base_where = list(scope_conditions)
        _count_base_params = list(scope_params)

        def _build_count_query(extra_condition=""):
            parts = list(_count_base_where)
            params = list(_count_base_params)
            if extra_condition:
                parts.append(extra_condition)
            where = ""
            if parts:
                where = "WHERE " + " AND ".join(parts)
            return where, params

        where_total, params_total = _build_count_query()
        cursor.execute(f"SELECT COUNT(*) FROM support_chats sc {where_total}", params_total)
        total_chats = cursor.fetchone()[0]

        where_active, params_active = _build_count_query("sc.status IN ('active', 'consultation')")
        cursor.execute(f"SELECT COUNT(*) FROM support_chats sc {where_active}", params_active)
        active_chats = cursor.fetchone()[0]

        where_pending, params_pending = _build_count_query("sc.status = 'pending'")
        cursor.execute(f"SELECT COUNT(*) FROM support_chats sc {where_pending}", params_pending)
        pending_chats = cursor.fetchone()[0]

        # Основной запрос: unread по единой модели read-state для текущего сотрудника.
        cursor.execute(f"""
            SELECT sc.id, sc.client_user_id, sc.specialist_id, sc.status, sc.profession,
                   sc.category, sc.request_type, sc.source, sc.created_at, sc.updated_at,
                   sc.accepted_at, sc.closed_at,
                   TIMESTAMPDIFF(SECOND, sc.created_at, COALESCE(sc.accepted_at, NOW())) AS wait_seconds,
                   CASE
                       WHEN sc.accepted_at IS NOT NULL AND sc.closed_at IS NOT NULL
                           THEN TIMESTAMPDIFF(SECOND, sc.accepted_at, sc.closed_at)
                       WHEN sc.accepted_at IS NOT NULL
                           THEN TIMESTAMPDIFF(SECOND, sc.accepted_at, NOW())
                       ELSE NULL
                   END AS process_seconds,
                   s.name AS specialist_name, s.profession AS specialist_profession,
                   u.first_name AS client_first_name, u.last_name AS client_last_name,
                   u.phone AS client_phone, u.inn AS client_inn, u.tg_username AS client_tg_username,
                   u.tg_user_id, u.max_user_id,
                   (SELECT COUNT(*) FROM support_chat_messages WHERE chat_id = sc.id) AS message_count,
                   (SELECT SUM(amount_rub) FROM support_chat_payments WHERE chat_id = sc.id AND status = 'CONFIRMED') AS total_paid,
                   (SELECT MAX(m.created_at) FROM support_chat_messages m WHERE m.chat_id = sc.id) AS last_message_at,
                   (
                       SELECT m.text
                       FROM support_chat_messages m
                       WHERE m.chat_id = sc.id
                       ORDER BY m.created_at DESC, m.id DESC
                       LIMIT 1
                   ) AS last_message_preview,
                   COALESCE(
                       (SELECT MAX(m.created_at) FROM support_chat_messages m WHERE m.chat_id = sc.id),
                       sc.updated_at,
                       sc.created_at
                   ) AS activity_at,
                   (
                       SELECT COUNT(*)
                       FROM support_chat_messages m
                       LEFT JOIN support_chat_read_state rs
                              ON rs.chat_id = sc.id AND rs.specialist_id = %s
                       WHERE m.chat_id = sc.id
                         AND m.sender = 0
                         AND m.id > COALESCE(rs.last_read_message_id, 0)
                   ) AS unread_count
            FROM support_chats sc
            LEFT JOIN specialists s ON s.id = sc.specialist_id
            LEFT JOIN users u ON u.id = sc.client_user_id
            {where}
            ORDER BY
                CASE WHEN unread_count > 0 THEN 0 ELSE 1 END ASC,
                activity_at DESC,
                sc.id DESC
            LIMIT 300
        """, [viewer_id, *params])
        columns = [col[0] for col in cursor.description]
        chats = [dict(zip(columns, row)) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT id, name, profession
            FROM specialists
            WHERE is_active = 1
              AND COALESCE(is_director, 0) = 0
            ORDER BY name
        """)
        specialist_columns = [col[0] for col in cursor.description]
        specialists = [dict(zip(specialist_columns, row)) for row in cursor.fetchall()]

    def _fmt_duration(seconds):
        if seconds is None:
            return None
        s = int(seconds)
        if s < 60:
            return f"{s} сек"
        if s < 3600:
            return f"{s // 60} мин"
        if s < 86400:
            h = s // 3600
            m = (s % 3600) // 60
            return f"{h} ч {m} мин" if m else f"{h} ч"
        d = s // 86400
        h = (s % 86400) // 3600
        return f"{d} д {h} ч" if h else f"{d} д"

    with connection.cursor() as cursor:
        for c in chats:
            c['wait_time'] = _fmt_duration(c.get('wait_seconds'))
            c['process_time'] = _fmt_duration(c.get('process_seconds'))
            c['has_new_client_response'] = int(c.get('unread_count') or 0) > 0
            c['last_activity_at'] = c.get('activity_at') or c.get('last_message_at') or c.get('updated_at') or c.get('created_at')
            preview = strip_tags(str(c.get('last_message_preview') or '')).strip()
            preview = re.sub(r"\s+", " ", preview)
            if len(preview) > 120:
                preview = preview[:117].rstrip() + '...'
            c['last_message_preview'] = preview
            c['is_partner_director'] = str(c.get('request_type') or '').lower() == 'partner_director'
            client_full_name = f"{(c.get('client_first_name') or '').strip()} {(c.get('client_last_name') or '').strip()}".strip()
            c['display_client_name'] = client_full_name or 'Клиент'
            c['display_client_prefix'] = ''
            if c['is_partner_director']:
                partner_meta = _resolve_partner_identity(
                    cursor,
                    client_user_id=c.get('client_user_id'),
                    client_phone=c.get('client_phone'),
                    fallback_name=client_full_name,
                )
                c['partner_name'] = partner_meta.get('partner_name') or 'Партнёр'
                c['display_client_prefix'] = 'Партнёр'
                c['display_client_name'] = c['partner_name']

    return render(request, 'bot_app/crm/chats_list.html', {
        'chats': chats,
        'specialists': specialists,
        'total_chats': total_chats,
        'active_chats': active_chats,
        'pending_chats': pending_chats,
        'shown_count': len(chats),
        'status_filter': status_filter,
        'profession_filter': profession_filter,
        'unread_filter': unread_filter,
        'client_filter': client_filter,
        'search_query': search_query,
        'active_menu': 'chats',
        'user': user,
        'is_director': is_director,
        'is_tech_support': is_tech_support,
    })

@login_required_custom
@tech_or_director_required
def api_crm_chats_unread_state(request):
    """API: lightweight realtime-state for unread indicators in CRM chats list."""
    status_filter = request.GET.get('status', '').strip()
    profession_filter = request.GET.get('profession', '').strip()
    client_filter = request.GET.get('client_id', '').strip()
    chat_ids_raw = request.GET.get('chat_ids', '').strip()

    chat_ids = []
    if chat_ids_raw:
        for item in chat_ids_raw.split(','):
            item = item.strip()
            if not item:
                continue
            try:
                chat_ids.append(int(item))
            except (TypeError, ValueError):
                continue

    with connection.cursor() as cursor:
        _ensure_support_chat_read_state_table(cursor)
        viewer_id = int(get_current_user(request)['id'])

        # Scope-фильтр для верхнего фильтра "Непрочитанные"
        conditions = []
        params = []
        if status_filter:
            conditions.append("sc.status = %s")
            params.append(status_filter)
        if profession_filter:
            try:
                conditions.append("sc.profession = %s")
                params.append(int(profession_filter))
            except (TypeError, ValueError):
                pass
        if client_filter:
            try:
                conditions.append("sc.client_user_id = %s")
                params.append(int(client_filter))
            except (TypeError, ValueError):
                pass

        where_scope = "WHERE " + " AND ".join(conditions) if conditions else ""
        cursor.execute(f"""
            SELECT COUNT(*)
            FROM support_chats sc
            {where_scope}
              {"AND" if where_scope else "WHERE"} EXISTS (
                  SELECT 1
                  FROM support_chat_messages m
                  LEFT JOIN support_chat_read_state rs
                         ON rs.chat_id = sc.id AND rs.specialist_id = %s
                  WHERE m.chat_id = sc.id
                    AND m.sender = 0
                    AND m.id > COALESCE(rs.last_read_message_id, 0)
              )
        """, [*params, viewer_id])
        unread_chats_count = int((cursor.fetchone() or [0])[0] or 0)

        chats = []
        if chat_ids:
            placeholders = ", ".join(["%s"] * len(chat_ids))
            cursor.execute(f"""
                SELECT
                    sc.id,
                    (
                        SELECT COUNT(*)
                        FROM support_chat_messages m
                        LEFT JOIN support_chat_read_state rs
                               ON rs.chat_id = sc.id AND rs.specialist_id = %s
                        WHERE m.chat_id = sc.id
                          AND m.sender = 0
                          AND m.id > COALESCE(rs.last_read_message_id, 0)
                    ) AS unread_count,
                    (
                        SELECT m.id
                        FROM support_chat_messages m
                        WHERE m.chat_id = sc.id
                        ORDER BY m.id DESC
                        LIMIT 1
                    ) AS last_message_id
                FROM support_chats sc
                WHERE sc.id IN ({placeholders})
            """, [viewer_id, *chat_ids])
            for row in cursor.fetchall():
                unread_count = int(row[1] or 0)
                chats.append({
                    'id': int(row[0]),
                    'unread_count': unread_count,
                    'has_new_client_response': unread_count > 0,
                    'last_message_id': int(row[2] or 0),
                })

    return JsonResponse({
        'success': True,
        'unread_chats_count': unread_chats_count,
        'chats': chats,
    })
