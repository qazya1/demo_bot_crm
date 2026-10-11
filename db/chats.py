from __future__ import annotations
import secrets
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from db.connection import (
    get_conn, _cur, _maybe_await, _begin_tx, _rollback_if_needed,
    _index_exists, _parse_dt_maybe,
    TOKEN_TTL_MIN, TOKEN_MAX_STALE_DAYS,
    IntegrityError,
)

# =========================================================
# Support chats / specialists (client <-> specialist)
# =========================================================

_MSG_EDIT_COLS_SUPPORTED: bool | None = None
_MSG_IS_READ_SUPPORTED: bool | None = None
logger = logging.getLogger(__name__)


async def _has_message_edit_columns(conn) -> bool:
    """Проверяет, есть ли в support_chat_messages колонки редактирования."""
    global _MSG_EDIT_COLS_SUPPORTED
    if _MSG_EDIT_COLS_SUPPORTED is not None:
        return _MSG_EDIT_COLS_SUPPORTED
    async with await _cur(conn) as c:
        await c.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM information_schema.columns
             WHERE table_schema = DATABASE()
               AND table_name = 'support_chat_messages'
               AND column_name IN ('edited_at', 'edited_by', 'is_edited')
            """
        )
        row = await c.fetchone() or {}
        _MSG_EDIT_COLS_SUPPORTED = int(row.get("cnt") or 0) == 3
    return bool(_MSG_EDIT_COLS_SUPPORTED)


async def _has_message_is_read_column(conn) -> bool:
    """Проверяет, есть ли в support_chat_messages колонка is_read."""
    global _MSG_IS_READ_SUPPORTED
    if _MSG_IS_READ_SUPPORTED is not None:
        return _MSG_IS_READ_SUPPORTED
    async with await _cur(conn) as c:
        await c.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM information_schema.columns
             WHERE table_schema = DATABASE()
               AND table_name = 'support_chat_messages'
               AND column_name = 'is_read'
            """
        )
        row = await c.fetchone() or {}
        _MSG_IS_READ_SUPPORTED = int(row.get("cnt") or 0) > 0
    return bool(_MSG_IS_READ_SUPPORTED)


async def _resolve_chat_client_user_id(
    *,
    client_user_id: int | str | None = None,
    internal_user_id: int | str | None = None,
    tg_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
    create: bool = False,
) -> int | None:
    from db.users import resolve_internal_user_id

    if internal_user_id is not None:
        return await resolve_internal_user_id(
            internal_user_id=internal_user_id,
            tg_user_id=tg_user_id,
            max_user_id=max_user_id,
            create=create,
        )

    if client_user_id is not None and str(client_user_id).strip() != "":
        resolved = await resolve_internal_user_id(internal_user_id=client_user_id)
        if resolved:
            return resolved
        resolved = await resolve_internal_user_id(tg_user_id=client_user_id, create=create)
        if resolved:
            return resolved
        resolved = await resolve_internal_user_id(max_user_id=client_user_id, create=create)
        if resolved:
            return resolved

    return await resolve_internal_user_id(
        tg_user_id=tg_user_id,
        max_user_id=max_user_id,
        create=create,
    )

async def ensure_support_chat_schema() -> None:
    """Создаёт нужные таблицы (безопасно) для модуля чатов."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:

            # specialists
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS specialists (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  phone VARCHAR(32) NULL,
                  tg_username VARCHAR(64) NULL,
                  tg_user_id BIGINT NULL,
                  max_user_id BIGINT NULL,
                  max_username VARCHAR(64) NULL,
                  name VARCHAR(128) NOT NULL,
                  profession INT NOT NULL,
                  is_active TINYINT NOT NULL DEFAULT 1,
                  phone_verified TINYINT NOT NULL DEFAULT 0,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  UNIQUE KEY uq_spec_tg_user_id (tg_user_id),
                  role VARCHAR(32) NOT NULL DEFAULT 'specialist',
                  KEY ix_spec_prof_active (profession, is_active)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            # chats (ВАЖНО: specialist_id NULL, fk -> ON DELETE SET NULL)
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS support_chats (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  client_user_id BIGINT NOT NULL,
                  specialist_id BIGINT NULL,
                  profession INT NOT NULL,
                  category VARCHAR(128) NULL,
                  request_type VARCHAR(128) NULL,
                  status ENUM('pending','active','closed','consultation') NOT NULL DEFAULT 'pending',
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  KEY ix_spec_status (specialist_id, status),
                  KEY ix_client_prof (client_user_id, profession)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            # messages
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS support_chat_messages (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  chat_id BIGINT NOT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  sender TINYINT NOT NULL,
                  text TEXT NOT NULL,
                  edited_at TIMESTAMP NULL DEFAULT NULL,
                  edited_by BIGINT NULL DEFAULT NULL,
                  is_edited TINYINT NOT NULL DEFAULT 0,
                  KEY ix_chat_time (chat_id, id),
                  CONSTRAINT fk_msg_chat FOREIGN KEY (chat_id) REFERENCES support_chats(id)
                    ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )


            # tokens (ссылки /chat?t=... для мини-приложения)
            await c.execute(
                '''
                CREATE TABLE IF NOT EXISTS support_chat_tokens (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  token VARCHAR(128) NOT NULL,
                  chat_id BIGINT NOT NULL,
                  role VARCHAR(32) NOT NULL,
                  actor_id VARCHAR(64) NULL,
                  expires_at DATETIME NOT NULL,
                  revoked_at DATETIME NULL,
                  replaced_by BIGINT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE KEY uq_support_chat_tokens_token (token),
                  KEY ix_support_chat_tokens_chat (chat_id),
                  KEY ix_support_chat_tokens_expires (expires_at),
                  KEY ix_support_chat_tokens_replaced_by (replaced_by),
                  CONSTRAINT fk_support_chat_token_chat FOREIGN KEY (chat_id) REFERENCES support_chats(id)
                    ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                '''
            )

            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS abonents (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  phone_e164 VARCHAR(20) NOT NULL,
                  display_name VARCHAR(128) NULL,
                  type ENUM('user','specialist') NOT NULL,
                  user_id INT NULL,
                  specialist_id BIGINT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  UNIQUE KEY uq_abonents_phone_e164 (phone_e164),
                  KEY ix_abonents_specialist (specialist_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            # На случай, если таблица уже есть в старом виде — докидываем недостающие колонки/индексы.
            # (MySQL не поддерживает ADD COLUMN IF NOT EXISTS во всех версиях, поэтому через try/except)
            for stmt in (
                "ALTER TABLE support_chat_tokens ADD COLUMN actor_id VARCHAR(64) NULL",
                "ALTER TABLE support_chat_tokens ADD COLUMN revoked_at DATETIME NULL",
                "ALTER TABLE support_chat_tokens ADD COLUMN replaced_by BIGINT NULL",
                "CREATE INDEX ix_support_chat_tokens_replaced_by ON support_chat_tokens(replaced_by)",
                "ALTER TABLE support_chat_messages ADD COLUMN edited_at TIMESTAMP NULL DEFAULT NULL",
                "ALTER TABLE support_chat_messages ADD COLUMN edited_by BIGINT NULL DEFAULT NULL",
                "ALTER TABLE support_chat_messages ADD COLUMN is_edited TINYINT NOT NULL DEFAULT 0",
                "ALTER TABLE support_chat_messages ADD COLUMN is_read TINYINT NOT NULL DEFAULT 1",
                "CREATE INDEX ix_msg_unread ON support_chat_messages(chat_id, sender, is_read)",
            ):
                try:
                    await c.execute(stmt)
                except Exception:
                    pass

            await _maybe_await(conn.commit())


def _normalize_phone_for_abonent(phone: str | None) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return f"+{digits}" if digits else ""


async def ensure_abonent_for_client(*, internal_user_id: int, phone: str, display_name: str | None = None) -> None:
    try:
        await ensure_support_chat_schema()
    except Exception:
        logger.exception("Failed to ensure support chat schema before client abonent upsert")
        raise

    normalized_phone = _normalize_phone_for_abonent(phone)
    if not normalized_phone:
        logger.warning("Skip client abonent upsert: empty/invalid phone (internal_user_id=%s)", internal_user_id)
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            try:
                await c.execute(
                    """
                    INSERT INTO abonents (phone_e164, display_name, type, user_id)
                    VALUES (%s, %s, 'user', %s)
                    ON DUPLICATE KEY UPDATE
                      display_name = COALESCE(VALUES(display_name), display_name),
                      user_id = COALESCE(VALUES(user_id), user_id),
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (normalized_phone, (display_name or "").strip() or None, int(internal_user_id)),
                )
                await _maybe_await(conn.commit())
            except Exception as exc:
                err_text = str(exc or "")
                if "Invalid owner" in err_text:
                    async with await _cur(conn) as c2:
                        await c2.execute(
                            "SELECT id, type, user_id, specialist_id FROM abonents WHERE phone_e164=%s LIMIT 1",
                            (normalized_phone,),
                        )
                        existing = await c2.fetchone() or {}
                    logger.warning(
                        "Skip client abonent upsert due owner constraint (internal_user_id=%s, phone=%s, existing=%s)",
                        internal_user_id, normalized_phone, existing
                    )
                    return
                logger.exception(
                    "Failed to upsert client abonent (internal_user_id=%s, phone=%s)",
                    internal_user_id, normalized_phone
                )
                raise


async def ensure_abonent_for_specialist(*, specialist_id: int, phone: str, display_name: str | None = None) -> None:
    try:
        await ensure_support_chat_schema()
    except Exception:
        logger.exception("Failed to ensure support chat schema before specialist abonent upsert")
        raise

    normalized_phone = _normalize_phone_for_abonent(phone)
    if not normalized_phone:
        logger.warning("Skip specialist abonent upsert: empty/invalid phone (specialist_id=%s)", specialist_id)
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            try:
                await c.execute(
                    """
                    INSERT INTO abonents (phone_e164, display_name, type, specialist_id)
                    VALUES (%s, %s, 'specialist', %s)
                    ON DUPLICATE KEY UPDATE
                      display_name = COALESCE(VALUES(display_name), display_name),
                      specialist_id = COALESCE(VALUES(specialist_id), specialist_id),
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (normalized_phone, (display_name or "").strip() or None, int(specialist_id)),
                )
                await _maybe_await(conn.commit())
            except Exception as exc:
                err_text = str(exc or "")
                if "Invalid owner" in err_text:
                    async with await _cur(conn) as c2:
                        await c2.execute(
                            "SELECT id, type, user_id, specialist_id FROM abonents WHERE phone_e164=%s LIMIT 1",
                            (normalized_phone,),
                        )
                        existing = await c2.fetchone() or {}
                    logger.warning(
                        "Skip specialist abonent upsert due owner constraint (specialist_id=%s, phone=%s, existing=%s)",
                        specialist_id, normalized_phone, existing
                    )
                    return
                logger.exception(
                    "Failed to upsert specialist abonent (specialist_id=%s, phone=%s)",
                    specialist_id, normalized_phone
                )
                raise




async def list_active_specialists(profession: int) -> List[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM specialists
                 WHERE profession=%s AND is_active=1 AND profession<>5
                 ORDER BY id
                """,
                (int(profession),),
            )
            return await c.fetchall() or []

async def list_directors() -> List[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM specialists
                 WHERE role="director" AND is_active=1
                 ORDER BY id
                """
            )
            return await c.fetchall() or []


async def get_least_busy_specialist(profession: int, exclude_ids: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
    """Выбирает наименее занятого: меньше активных/ожидающих чатов."""
    exclude_ids = exclude_ids or []
    async with get_conn() as conn:
        async with await _cur(conn) as c:

            params: List[Any] = [int(profession)]
            where_excl = ""
            if exclude_ids:
                where_excl = " AND s.id NOT IN (%s)" % (",".join(["%s"] * len(exclude_ids)))
                params.extend([int(x) for x in exclude_ids])

            await c.execute(
                f"""
                SELECT
                  s.*,
                  COALESCE(cnt.cnt, 0) AS active_clients
                FROM specialists s
                LEFT JOIN (
                  SELECT specialist_id, COUNT(*) AS cnt
                    FROM support_chats
                   WHERE status IN ('pending','active')
                   GROUP BY specialist_id
                ) cnt ON cnt.specialist_id = s.id
                WHERE s.profession=%s AND s.is_active=1 AND s.profession<>5 {where_excl}
                ORDER BY active_clients ASC, s.id ASC
                LIMIT 1
                """,
                tuple(params),
            )
            return await c.fetchone()

async def create_support_chat(client_user_id: str, specialist_id: int, profession: int,
                        request_type: str | None, category: str | None,
                        source: str | None = None) -> int:
    """
    Идемпотентно создаёт чат. Если уже есть pending/active по (client_user_id, profession) — вернём существующий id.
    """
    resolved_client_user_id = await _resolve_chat_client_user_id(client_user_id=client_user_id)
    if not resolved_client_user_id:
        raise ValueError(f"Cannot resolve client_user_id={client_user_id!r} to users.id")
    profession = int(profession)
    specialist_id = int(specialist_id)

    async with get_conn() as conn:
        async with await _cur(conn) as c:

            # 1) если уже есть pending/active — вернуть его
            await c.execute(
                """
                SELECT id
                FROM support_chats
                WHERE client_user_id=%s AND profession=%s AND status IN ('pending','active')
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(resolved_client_user_id), profession),
            )
            row = await c.fetchone()
            if row and row.get("id"):
                return int(row["id"])

            # 2) иначе создаём новый pending
            await c.execute(
                """
                INSERT INTO support_chats (client_user_id, specialist_id, profession, request_type, category, status, source, created_at)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, NOW())
                """,
                (int(resolved_client_user_id), specialist_id, profession, request_type, category, source),
            )
            await _maybe_await(conn.commit())
            return int(c.lastrowid)

async def link_specialist_tg_user(specialist_id: int, tg_user_id: int, tg_username: str | None = None) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if tg_username:
                await c.execute(
                    "UPDATE specialists SET tg_user_id=%s, tg_username=%s WHERE id=%s",
                    (int(tg_user_id), tg_username, int(specialist_id)),
                )
            else:
                await c.execute(
                    "UPDATE specialists SET tg_user_id=%s WHERE id=%s",
                    (int(tg_user_id), int(specialist_id)),
                )
            await _maybe_await(conn.commit())


async def set_chat_status(chat_id: int, status: str) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            extra = ""
            if status == "closed":
                extra = ", closed_at=COALESCE(closed_at, CURRENT_TIMESTAMP)"
            elif status in ("active", "consultation"):
                extra = ", accepted_at=COALESCE(accepted_at, CURRENT_TIMESTAMP)"
            await c.execute(
                f"UPDATE support_chats SET status=%s, updated_at=CURRENT_TIMESTAMP{extra} WHERE id=%s",
                (status, int(chat_id)),
            )
            await _maybe_await(conn.commit())


# ===================== ORDER CHATS (chat about a specific order) =====================

ORDER_CHAT_REQUEST_TYPE = "order"


async def get_order_chat(order_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает чат по заказу (если создан)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT *
                  FROM support_chats
                 WHERE request_type=%s AND category=%s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (ORDER_CHAT_REQUEST_TYPE, str(int(order_id))),
            )
            return await c.fetchone()


async def ensure_order_chat(*, order_id: int, client_user_id: int, director_specialist_id: int, source: str | None = None) -> int:
    """
    Идемпотентно создаёт чат "по заказу".

    В отличие от create_support_chat(), здесь допустимо иметь несколько открытых чатов
    у одного клиента в рамках одной профессии, поэтому ключом выступает order_id.

    Создаём чат сразу в статусе 'active', чтобы директор мог написать первым.
    """
    order_id = int(order_id)
    resolved_client_user_id = await _resolve_chat_client_user_id(client_user_id=client_user_id)
    if not resolved_client_user_id:
        raise ValueError(f"Cannot resolve client_user_id={client_user_id!r} to users.id")
    director_specialist_id = int(director_specialist_id)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id
                  FROM support_chats
                 WHERE request_type=%s AND category=%s
                   AND status IN ('pending','active','consultation')
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (ORDER_CHAT_REQUEST_TYPE, str(order_id)),
            )
            row = await c.fetchone()
            if row and row.get("id"):
                return int(row["id"])

            await c.execute(
                """
                INSERT INTO support_chats (client_user_id, specialist_id, profession, request_type, category, status, source, created_at)
                VALUES (%s, %s, %s, %s, %s, 'active', %s, NOW())
                """,
                (
                    int(resolved_client_user_id),
                    director_specialist_id,
                    3,  # profession=3 (директор)
                    ORDER_CHAT_REQUEST_TYPE,
                    str(order_id),
                    source,
                ),
            )
            await _maybe_await(conn.commit())
            return int(c.lastrowid)


async def delete_order_chat(order_id: int) -> None:
    """Физически удаляет чат по заказу (включая сообщения/токены через каскады)."""
    order_id = int(order_id)
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "DELETE FROM support_chats WHERE request_type=%s AND category=%s",
                (ORDER_CHAT_REQUEST_TYPE, str(order_id)),
            )
            await _maybe_await(conn.commit())




async def get_active_chat_for_client(client_user_id: str, profession: int) -> Optional[Dict[str, Any]]:
    resolved_client_user_id = await _resolve_chat_client_user_id(client_user_id=client_user_id)
    if not resolved_client_user_id:
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT * FROM support_chats
                 WHERE client_user_id=%s AND profession=%s AND status='active'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (int(resolved_client_user_id), int(profession)),
            )
            return await c.fetchone()


async def get_open_chat_for_client(
    client_user_id: str,
    profession: int,
    request_type: str | None = None,
    category: str | None = None,
    category_prefix: str | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Последний открытый чат клиента по профессии (pending/active) с доп. фильтрами.
    Используем, чтобы не плодить дубликаты (например, консультации из магазина).
    """
    resolved_client_user_id = await _resolve_chat_client_user_id(client_user_id=client_user_id)
    if not resolved_client_user_id:
        return None
    profession = int(profession)

    where = [
        "client_user_id=%s",
        "profession=%s",
        "status IN ('pending','active','consultation')",
    ]
    params: list[Any] = [int(resolved_client_user_id), profession]

    if request_type is not None:
        where.append("request_type=%s")
        params.append(str(request_type))

    if category is not None:
        where.append("category=%s")
        params.append(str(category))
    elif category_prefix is not None:
        where.append("category LIKE %s")
        params.append(str(category_prefix) + "%")

    sql = "SELECT * FROM support_chats WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT 1"

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(sql, tuple(params))
            return await c.fetchone()


async def get_chat_by_id(chat_id: int) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT ch.*,
                       COALESCE(
                         NULLIF(u.first_name, ''),
                        'Клиент'
                       ) AS client_name,
                       u.phone AS client_phone,
                       u.inn AS client_inn,
                       COALESCE(u.we_added, 0) AS client_we_added
                  FROM support_chats ch
                  LEFT JOIN users u ON u.id = ch.client_user_id
                 WHERE ch.id=%s
                 LIMIT 1
                """,
                (int(chat_id),),
            )
            return await c.fetchone()



async def add_chat_message(
    chat_id: int,
    sender: int,
    text: str | None = None,
    *,
    msg_type: str | None = None,
    content: str | None = None,
) -> int | None:
    """
    Backward/forward compatible:
    - old calls: add_chat_message(chat_id, sender, "hello")
    - new calls: add_chat_message(chat_id, sender=0, msg_type="text", content="hello")
    Сейчас в БД хранится только text, msg_type пока игнорим (можно позже расширить таблицу).
    """
    # поддержка нового стиля
    if text is None:
        text = content or ""

    text = str(text or "").strip()
    if not text:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if await _has_message_is_read_column(conn):
                await c.execute(
                    """
                    INSERT INTO support_chat_messages (chat_id, sender, text, is_read)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (int(chat_id), int(sender), text, 1 if int(sender) != 0 else 0),
                )
            else:
                await c.execute(
                    """
                    INSERT INTO support_chat_messages (chat_id, sender, text)
                    VALUES (%s, %s, %s)
                    """,
                    (int(chat_id), int(sender), text),
                )
            await _maybe_await(conn.commit())
            return int(getattr(c, 'lastrowid', 0) or 0)

async def get_chat_messages(chat_id: int, limit: int = 100, offset: int = 0):
    """Получает сообщения с информацией о редактировании."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            has_edit_cols = await _has_message_edit_columns(conn)
            if has_edit_cols:
                sql = """
                SELECT id, chat_id, created_at, sender, text, edited_at, edited_by, is_edited
                  FROM support_chat_messages
                 WHERE chat_id=%s
                 ORDER BY id DESC
                 LIMIT %s OFFSET %s
                """
            else:
                sql = """
                SELECT id, chat_id, created_at, sender, text,
                       NULL AS edited_at, NULL AS edited_by, 0 AS is_edited
                  FROM support_chat_messages
                 WHERE chat_id=%s
                 ORDER BY id DESC
                 LIMIT %s OFFSET %s
                """
            await c.execute(
                sql,
                (int(chat_id), int(limit), int(offset)),
            )
            rows = await c.fetchall() or []
            # если в боте ожидается хронологический порядок - переворачиваем:
            rows.reverse()
            return rows

async def get_chat_messages_after(chat_id: int, after_id: int = 0, limit: int = 50) -> list[dict]:
    """Получает сообщения после указанного ID с информацией о редактировании."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            has_edit_cols = await _has_message_edit_columns(conn)
            if has_edit_cols:
                sql = """
                SELECT id, chat_id, created_at, sender, text, edited_at, edited_by, is_edited
                  FROM support_chat_messages
                 WHERE chat_id=%s AND id > %s
                 ORDER BY id ASC
                 LIMIT %s
                """
            else:
                sql = """
                SELECT id, chat_id, created_at, sender, text,
                       NULL AS edited_at, NULL AS edited_by, 0 AS is_edited
                  FROM support_chat_messages
                 WHERE chat_id=%s AND id > %s
                 ORDER BY id ASC
                 LIMIT %s
                """
            await c.execute(
                sql,
                (int(chat_id), int(after_id), int(limit)),
            )
            return await c.fetchall() or []



async def update_chat_message(
    message_id: int,
    new_text: str,
    editor_user_id: int,
    is_director: bool = False,
) -> bool:
    """
    Обновляет текст сообщения.
    
    Args:
        message_id: ID сообщения для обновления
        new_text: Новый текст сообщения
        editor_user_id: ID пользователя, который редактирует (internal users.id)
        is_director: True если редактор - директор (может редактировать любые сообщения)
    
    Returns:
        True если успешно обновлено, False если нет прав или сообщение не найдено
    """
    new_text = (new_text or "").strip()
    if not new_text:
        return False
    
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Получаем сообщение и чат для проверки прав
            await c.execute(
                """
                SELECT m.id, m.chat_id, m.sender, m.text, ch.specialist_id, ch.status
                FROM support_chat_messages m
                JOIN support_chats ch ON ch.id = m.chat_id
                WHERE m.id = %s
                LIMIT 1
                FOR UPDATE
                """,
                (int(message_id),),
            )
            row = await c.fetchone()
            if not row:
                return False
            
            chat_id = int(row["chat_id"])
            original_sender = int(row["sender"])
            specialist_id = row.get("specialist_id")
            
            # Проверка прав:
            # 1. Автор сообщения может редактировать свои сообщения
            # 2. Директор может редактировать любые сообщения
            # sender: 0 = client, 1 = specialist
            # editor_user_id - это internal users.id
            
            # Получаем client_user_id из чата для сравнения
            await c.execute(
                "SELECT client_user_id FROM support_chats WHERE id = %s",
                (chat_id,),
            )
            chat_row = await c.fetchone()
            if not chat_row:
                return False
            
            client_user_id = int(chat_row.get("client_user_id") or 0)
            
            # Определяем может ли пользователь редактировать
            can_edit = False
            
            if is_director:
                # Директор может редактировать любые сообщения
                can_edit = True
            elif original_sender == 0:
                # Сообщение от клиента - может редактировать только клиент
                can_edit = (int(editor_user_id) == client_user_id)
            else:
                # Сообщение от специалиста - может редактировать только назначенный специалист
                if specialist_id:
                    # Получаем internal user ID специалиста
                    spec_internal_id = await resolve_internal_user_id(internal_user_id=int(specialist_id))
                    can_edit = (int(editor_user_id) == int(spec_internal_id or 0))
            
            if not can_edit:
                return False
            
            # Обновляем сообщение
            has_edit_cols = await _has_message_edit_columns(conn)
            if has_edit_cols:
                await c.execute(
                    """
                    UPDATE support_chat_messages
                    SET text = %s,
                        edited_at = CURRENT_TIMESTAMP,
                        edited_by = %s,
                        is_edited = 1
                    WHERE id = %s
                    """,
                    (new_text, int(editor_user_id), int(message_id)),
                )
            else:
                await c.execute(
                    """
                    UPDATE support_chat_messages
                    SET text = %s
                    WHERE id = %s
                    """,
                    (new_text, int(message_id)),
                )
            await _maybe_await(conn.commit())
            return c.rowcount > 0


async def get_message_by_id(message_id: int) -> Optional[Dict[str, Any]]:
    """Получает сообщение по ID с информацией о редактировании."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            has_edit_cols = await _has_message_edit_columns(conn)
            if has_edit_cols:
                sql = """
                SELECT id, chat_id, created_at, sender, text, edited_at, edited_by, is_edited
                FROM support_chat_messages
                WHERE id = %s
                LIMIT 1
                """
            else:
                sql = """
                SELECT id, chat_id, created_at, sender, text,
                       NULL AS edited_at, NULL AS edited_by, 0 AS is_edited
                FROM support_chat_messages
                WHERE id = %s
                LIMIT 1
                """
            await c.execute(
                sql,
                (int(message_id),),
            )
            return await c.fetchone()


async def count_chat_messages(chat_id: int) -> int:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT COUNT(*) AS c FROM support_chat_messages WHERE chat_id=%s", (int(chat_id),))
            return int(((await c.fetchone()) or {}).get("c") or 0)


async def count_chat_messages_by_sender(chat_id: int, sender: int) -> int:
    """Количество сообщений в чате от конкретного отправителя (0=client, 1=specialist)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT COUNT(*) AS c FROM support_chat_messages WHERE chat_id=%s AND sender=%s",
                (int(chat_id), int(sender)),
            )
            return int(((await c.fetchone()) or {}).get("c") or 0)


async def close_chat(chat_id: int) -> None:
    await set_chat_status(int(chat_id), "closed")

async def reset_chat_close_votes(chat_id: int) -> None:
    """Сбрасывает запросы на закрытие с обеих сторон (если чат ещё не закрыт)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE support_chats
                   SET close_req_client=0,
                       close_req_agent=0,
                       close_req_client_at=NULL,
                       close_req_agent_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE id=%s AND status<>'closed'
                """,
                (int(chat_id),),
            )
            await _maybe_await(conn.commit())

async def list_recent_support_chats(limit: int = 20):
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, status, profession, category, request_type, created_at, updated_at
                  FROM support_chats
                 ORDER BY id DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return await c.fetchall() or []

async def request_chat_close(chat_id: int, *, by: str) -> Dict[str, Any]:
    """
    Фиксирует согласие на завершение обращения от одной стороны.
    Чат закрывается ТОЛЬКО когда согласны обе стороны.

    by: 'client' | 'agent'

    Return:
      { ok: bool, status: str, close_req_client: int, close_req_agent: int, just_closed: bool }
    """
    by = (by or "").strip().lower()
    if by not in ("client", "agent"):
        return {"ok": False, "error": "bad_by"}

    my_col = "close_req_client" if by == "client" else "close_req_agent"
    my_at = "close_req_client_at" if by == "client" else "close_req_agent_at"
    other_col = "close_req_agent" if by == "client" else "close_req_client"

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # атомарно: блокируем строку заявки
            await c.execute(
                """
                SELECT id, status, request_type, close_req_client, close_req_agent
                  FROM support_chats
                 WHERE id=%s
                 FOR UPDATE
                """,
                (int(chat_id),),
            )
            row = await c.fetchone() or {}
            if not row:
                await _maybe_await(conn.rollback())
                return {"ok": False, "error": "not_found"}
            if str(row.get("request_type") or "").strip().lower() == PARTNER_DIRECTOR_REQUEST_TYPE:
                await _maybe_await(conn.rollback())
                return {
                    "ok": False,
                    "error": "partner_director_not_closable",
                    "status": str(row.get("status") or ""),
                    "close_req_client": int(row.get("close_req_client") or 0),
                    "close_req_agent": int(row.get("close_req_agent") or 0),
                    "just_closed": False,
                }

            status = str(row.get("status") or "")
            if status == "closed":
                await _maybe_await(conn.rollback())
                return {
                    "ok": True,
                    "status": "closed",
                    "close_req_client": int(row.get("close_req_client") or 0),
                    "close_req_agent": int(row.get("close_req_agent") or 0),
                    "just_closed": False,
                }

            # ставим флаг согласия этой стороны
            await c.execute(
                f"UPDATE support_chats SET {my_col}=1, {my_at}=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                (int(chat_id),),
            )

            other = int(row.get(other_col) or 0)
            just_closed = False
            if other == 1:
                # обе стороны согласились
                await c.execute(
                    "UPDATE support_chats SET status='closed', updated_at=CURRENT_TIMESTAMP, closed_at=COALESCE(closed_at, CURRENT_TIMESTAMP) WHERE id=%s",
                    (int(chat_id),),
                )
                just_closed = True

            # перечитаем текущие флаги (для UI)
            await c.execute("SELECT status, close_req_client, close_req_agent FROM support_chats WHERE id=%s", (int(chat_id),))
            cur = await c.fetchone() or {}
            await _maybe_await(conn.commit())
            return {
                "ok": True,
                "status": str(cur.get("status") or status),
                "close_req_client": int(cur.get("close_req_client") or 0),
                "close_req_agent": int(cur.get("close_req_agent") or 0),
                "just_closed": bool(just_closed),
            }

async def delete_chat(chat_id: int) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("DELETE FROM support_chats WHERE id=%s", (int(chat_id),))
            await _maybe_await(conn.commit())

async def count_specialists_for_profession_any(profession: int) -> int:
    """Сколько вообще специалистов этой профессии заведено (без фильтров верификации)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT COUNT(*) AS c FROM specialists WHERE profession=%s",
                (int(profession),),
            )
            return int(((await c.fetchone()) or {}).get("c") or 0)


async def count_specialists_for_profession_ready(profession: int) -> int:
    """Сколько готовых специалистов (телефон подтвержден + активен + tg_user_id есть)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COUNT(*) AS c
                  FROM specialists
                 WHERE profession=%s
                   AND is_active=1
                   AND profession<>5
                   AND phone_verified=1
                   AND tg_user_id IS NOT NULL
                   AND profession<>5
                """,
                (int(profession),),
            )
            return int(((await c.fetchone()) or {}).get("c") or 0)






def is_partner_specialist(spec: Optional[Dict[str, Any]]) -> bool:
    return bool(spec) and int((spec or {}).get("profession") or 0) == 5

async def get_specialist_by_tg(*, tg_user_id: int | None, tg_username: str | None) -> Optional[Dict[str, Any]]:
    """
    Ищем специалиста по tg_user_id (приоритет) или по username.
    Если нашли по username и tg_user_id в БД пустой — привязываем автоматически.
    """
    tg_username = (tg_username or "").strip().lstrip("@") or None

    async with get_conn() as conn:
        async with await _cur(conn) as c:

            # 1) приоритетно по tg_user_id
            if tg_user_id:
                await c.execute(
                    "SELECT * FROM specialists WHERE tg_user_id=%s AND is_active=1 LIMIT 1",
                    (int(tg_user_id),),
                )
                row = await c.fetchone()
                if row:
                    # обновим username на всякий
                    if tg_username and (row.get("tg_username") != tg_username):
                        await c.execute(
                            "UPDATE specialists SET tg_username=%s WHERE id=%s",
                            (tg_username, int(row["id"])),
                        )
                        await _maybe_await(conn.commit())
                        row["tg_username"] = tg_username
                    return row

            # 2) иначе по username
            if tg_username:
                await c.execute(
                    "SELECT * FROM specialists WHERE tg_username=%s AND is_active=1 LIMIT 1",
                    (tg_username,),
                )
                row = await c.fetchone()
                if not row:
                    return None

                # !!! ключевое: если tg_user_id пришёл, а в БД пусто/другое — привязываем
                if tg_user_id and (row.get("tg_user_id") is None or int(row.get("tg_user_id")) != int(tg_user_id)):
                    await c.execute(
                        "UPDATE specialists SET tg_user_id=%s, tg_username=%s WHERE id=%s",
                        (int(tg_user_id), tg_username, int(row["id"])),
                    )
                    await _maybe_await(conn.commit())
                    row["tg_user_id"] = int(tg_user_id)
                    row["tg_username"] = tg_username

                return row

            return None


async def set_specialist_phone_verified(specialist_id: int, verified: bool) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("UPDATE specialists SET phone_verified=%s WHERE id=%s", (1 if verified else 0, int(specialist_id)))
            await _maybe_await(conn.commit())




async def reassign_chat_to_next_specialist(chat_id: int) -> tuple[bool, Optional[Dict[str, Any]]]:
    """
    Выбираем следующего наименее занятого спеца той же профессии,
    исключая всех, кто уже отказался + текущего спеца.
    Обновляем support_chats.specialist_id.
    Возвращаем (ok, specialist_row).
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:

            # FOR UPDATE: блокируем строку чата на время транзакции,
            # чтобы параллельный try_accept_chat() не был перезаписан.
            await c.execute("SELECT * FROM support_chats WHERE id=%s LIMIT 1 FOR UPDATE", (int(chat_id),))
            chat = await c.fetchone()
            if not chat:
                await _maybe_await(conn.commit())
                return False, None

            profession = int(chat.get("profession") or 0)
            current_spec = int(chat.get("specialist_id") or 0)

            await c.execute("SELECT specialist_id FROM support_chat_declines WHERE chat_id=%s", (int(chat_id),))
            declined = [int(r["specialist_id"]) for r in (await c.fetchall() or [])]
            exclude = set(declined + ([current_spec] if current_spec else []))

            # наименее занятый
            params: List[Any] = [profession]
            where_excl = ""
            if exclude:
                where_excl = " AND s.id NOT IN (%s)" % ",".join(["%s"] * len(exclude))
                params.extend(list(exclude))

            await c.execute(
                f"""
                SELECT
                  s.*,
                  COALESCE(cnt.cnt, 0) AS active_clients
                FROM specialists s
                LEFT JOIN (
                  SELECT specialist_id, COUNT(*) AS cnt
                    FROM support_chats
                   WHERE status IN ('pending','active')
                   GROUP BY specialist_id
                ) cnt ON cnt.specialist_id = s.id
                WHERE s.profession=%s AND s.is_active=1 AND s.phone_verified=1 {where_excl}
                ORDER BY active_clients ASC, s.id ASC
                LIMIT 1
                """,
                tuple(params),
            )
            new_spec = await c.fetchone()
            if not new_spec:
                await _maybe_await(conn.commit())
                return False, None

            # WHERE status='pending' — страховка: если чат уже принят (status='active'),
            # UPDATE не затронет строку и мы вернём False вместо перезаписи.
            await c.execute(
                "UPDATE support_chats SET specialist_id=%s, status='pending', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=%s AND status='pending'",
                (int(new_spec["id"]), int(chat_id)),
            )
            await _maybe_await(conn.commit())
            if c.rowcount == 0:
                return False, None
            return True, new_spec


async def set_client_last_specialist(client_user_id: str, profession: int, specialist_id: int) -> None:
    """
    В старой схеме обновлялись clients.accountant_id / clients.support_id.
    В новой схеме таблица clients объединена с users.
    Пока делаем no-op, так как этих полей нет в новой схеме users.
    """
    # В новой схеме users не имеет полей accountant_id/support_id
    # Если нужно - можно добавить эти поля в users и раскомментировать код ниже
    pass


async def create_chat_payment(
    *,
    chat_id: int,
    specialist_id: int,
    amount_rub: int,
    payment_id: str,
    pay_url: str,
    status: str,
    bonus_points_applied: int = 0,
    bonus_discount_rub: int = 0,
    cash_amount_rub: int = 0,
    bonus_reservation_key: str | None = None,
    payment_method: str = "CASH_ONLY",
) -> None:
    params = (
        int(chat_id), int(specialist_id), int(amount_rub), str(payment_id), str(pay_url), str(status),
        int(bonus_points_applied or 0), int(bonus_discount_rub or 0), int(cash_amount_rub or 0),
        (str(bonus_reservation_key) if bonus_reservation_key else None), str(payment_method or "CASH_ONLY"),
    )

    async def _insert_payment() -> None:
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    """
                    INSERT INTO support_chat_payments
                        (chat_id, specialist_id, amount_rub, payment_id, pay_url, status,
                         bonus_points_applied, bonus_discount_rub, cash_amount_rub, bonus_reservation_key, payment_method)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      chat_id=VALUES(chat_id),
                      specialist_id=VALUES(specialist_id),
                      amount_rub=VALUES(amount_rub),
                      pay_url=VALUES(pay_url),
                      status=VALUES(status),
                      bonus_points_applied=VALUES(bonus_points_applied),
                      bonus_discount_rub=VALUES(bonus_discount_rub),
                      cash_amount_rub=VALUES(cash_amount_rub),
                      bonus_reservation_key=VALUES(bonus_reservation_key),
                      payment_method=VALUES(payment_method)
                    """,
                    params,
                )
                await _maybe_await(conn.commit())

    try:
        await _insert_payment()
    except Exception as e:
        msg = str(e).lower()
        # Самовосстановление для старых БД, где не докатились колонки бонусной оплаты.
        if "unknown column" in msg and "support_chat_payments" in msg:
            await ensure_agent_chat_schema()
            await _insert_payment()
            return
        raise


async def set_payment_status_by_payment_id(payment_id: str, status: str) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE support_chat_payments
                   SET status=%s,
                       paid_at=CASE WHEN %s='CONFIRMED' THEN COALESCE(paid_at, CURRENT_TIMESTAMP) ELSE paid_at END
                 WHERE payment_id=%s
                """,
                (str(status), str(status), str(payment_id)),
            )
            await _maybe_await(conn.commit())


async def get_payment_by_payment_id(payment_id: str) -> Optional[Dict[str, Any]]:
    """
    Возвращаем платёж + tg_user_id специалиста, чтобы уведомлять его.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT p.*,
                       s.tg_user_id AS specialist_tg_user_id
                  FROM support_chat_payments p
             LEFT JOIN specialists s ON s.id = p.specialist_id
                 WHERE p.payment_id=%s
                 LIMIT 1
                """,
                (str(payment_id),),
            )
            return await c.fetchone()


async def has_chat_payment_success_message(chat_id: int, payment_id: str) -> bool:
    marker = f"%ID платежа: {str(payment_id)}%"
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT 1
                  FROM support_chat_messages
                 WHERE chat_id=%s
                   AND text LIKE %s
                 LIMIT 1
                """,
                (int(chat_id), marker),
            )
            return bool(await c.fetchone())




async def list_chat_payments_awaiting_confirmation(max_age_minutes: int = 24 * 60) -> List[Dict[str, Any]]:
    """Платежи чатов, ожидающие подтверждения (для фоновой дообработки)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT p.*
                  FROM support_chat_payments p
                 WHERE p.status IN ('NEW', 'BONUS_RESERVED')
                   AND p.created_at >= (NOW() - INTERVAL %s MINUTE)
                 ORDER BY p.id DESC
                """,
                (int(max_age_minutes),),
            )
            return await c.fetchall()


async def list_confirmed_chat_payments_without_order(max_age_minutes: int = 24 * 60) -> List[Dict[str, Any]]:
    """Подтверждённые оплаты чата, по которым ещё не создана сделка orders."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT p.*
                  FROM support_chat_payments p
             LEFT JOIN orders o ON o.payment_id = p.payment_id
                 WHERE p.status = 'CONFIRMED'
                   AND o.id IS NULL
                   AND p.paid_at IS NOT NULL
                   AND p.paid_at >= (NOW() - INTERVAL %s MINUTE)
                 ORDER BY p.id DESC
                """,
                (int(max_age_minutes),),
            )
            return await c.fetchall()


async def list_recent_confirmed_chat_payments(max_age_minutes: int = 24 * 60) -> List[Dict[str, Any]]:
    """Подтверждённые оплаты чата за период (для repair-прохода post-payment pipeline)."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT p.*
                  FROM support_chat_payments p
                 WHERE p.status = 'CONFIRMED'
                   AND p.paid_at IS NOT NULL
                   AND p.paid_at >= (NOW() - INTERVAL %s MINUTE)
                 ORDER BY p.id DESC
                """,
                (int(max_age_minutes),),
            )
            return await c.fetchall()


async def get_latest_chat_payment(chat_id: int, statuses: tuple[str, ...] = ("NEW", "BONUS_RESERVED")) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            placeholders = ",".join(["%s"] * len(statuses))
            await c.execute(
                f"""
                SELECT p.*,
                       s.tg_user_id AS specialist_tg_user_id
                  FROM support_chat_payments p
                  JOIN specialists s ON s.id = p.specialist_id
                 WHERE p.chat_id=%s
                   AND p.status IN ({placeholders})
                 ORDER BY p.id DESC
                 LIMIT 1
                """,
                (int(chat_id), *tuple(str(s) for s in statuses)),
            )
            return await c.fetchone()

async def list_chats_for_client(
    client_user_id: str | int | None = None,
    include_closed: bool = False,
    *,
    internal_user_id: str | int | None = None,
    tg_user_id: str | int | None = None,
    max_user_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    resolved_client_user_id = await _resolve_chat_client_user_id(
        client_user_id=client_user_id,
        internal_user_id=internal_user_id,
        tg_user_id=tg_user_id,
        max_user_id=max_user_id,
    )
    if not resolved_client_user_id:
        return []
    statuses = ("pending", "active", "consultation", "test") if not include_closed else ("pending", "active", "closed", "consultation", "test")
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"""
                SELECT
                  ch.*,
                  s.name AS specialist_name,
                  s.tg_username AS specialist_tg_username
                FROM support_chats ch
                LEFT JOIN specialists s ON s.id = ch.specialist_id
                WHERE ch.client_user_id=%s AND ch.status IN ({",".join(["%s"] * len(statuses))})
                ORDER BY ch.updated_at DESC, ch.id DESC
                """,
                (int(resolved_client_user_id), *statuses),
            )
            return await c.fetchall() or []






async def decline_chat(chat_id: int, specialist_id: int) -> None:
    """
    Специалист отказался: помечаем, чтобы эта заявка не показывалась ему в пуле.
    Таблица support_chat_declines уже есть в проекте.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            try:
                await c.execute(
                    """
                    INSERT IGNORE INTO support_chat_declines (chat_id, specialist_id)
                    VALUES (%s, %s)
                    """,
                    (int(chat_id), int(specialist_id)),
                )
                await _maybe_await(conn.commit())
            except Exception:
                # если таблица/индексы отличаются — не валим бота
                pass



async def get_specialist_by_id(specialist_id: int) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT * FROM specialists WHERE id=%s LIMIT 1", (int(specialist_id),))
            return await c.fetchone()





# ====== SUPPORT CHAT: offer message tracking (hide Accept/Decline menus) ======

async def ensure_offer_schema() -> None:
    """Храним message_id сообщений-офферов у специалистов, чтобы можно было убрать inline-меню после принятия."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                CREATE TABLE IF NOT EXISTS support_chat_offers (
                  chat_id BIGINT NOT NULL,
                  specialist_id BIGINT NOT NULL,
                  tg_chat_id BIGINT NOT NULL,
                  message_id BIGINT NOT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (chat_id, specialist_id, message_id),
                  KEY ix_offer_chat (chat_id),
                  KEY ix_offer_spec (specialist_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            await _maybe_await(conn.commit())


async def add_chat_offer(chat_id: int, specialist_id: int, tg_chat_id: int, message_id: int) -> None:
    try:
        await ensure_offer_schema()
    except Exception:
        pass
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT IGNORE INTO support_chat_offers (chat_id, specialist_id, tg_chat_id, message_id)
                VALUES (%s, %s, %s, %s)
                """,
                (int(chat_id), int(specialist_id), int(tg_chat_id), int(message_id)),
            )
            await _maybe_await(conn.commit())


async def list_chat_offers(chat_id: int) -> List[Dict[str, Any]]:
    try:
        await ensure_offer_schema()
    except Exception:
        pass
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT chat_id, specialist_id, tg_chat_id, message_id
                  FROM support_chat_offers
                 WHERE chat_id=%s
                """,
                (int(chat_id),),
            )
            return await c.fetchall() or []


async def delete_chat_offers(chat_id: int) -> None:
    try:
        await ensure_offer_schema()
    except Exception:
        pass
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("DELETE FROM support_chat_offers WHERE chat_id=%s", (int(chat_id),))
            await _maybe_await(conn.commit())

# ====== SUPPORT CHAT: helpers for new logic (broadcast + first accept) ======


async def ensure_agent_chat_schema() -> None:
    """
    Миграции для чатов/спецов (безопасно для существующих данных).
    - phone_verified в specialists
    - specialist_id NULL в support_chats (чтобы заявка могла быть "общей" pending)
    - удаляем кривой UNIQUE uq_active_chat (который даёт Duplicate entry при закрытии)
    - request_type в support_chats
    - support_chat_declines
    - support_chat_payments
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:

            # phone_verified
            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'specialists'
                   AND COLUMN_NAME = 'phone_verified'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE specialists ADD COLUMN phone_verified TINYINT NOT NULL DEFAULT 0")

            # role (director/specialist)
            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'specialists'
                   AND COLUMN_NAME = 'role'
            """)
            row = await c.fetchone()
            if int((row or {}).get("c") or 0) == 0:
                await c.execute(
                    "ALTER TABLE specialists ADD COLUMN role VARCHAR(32) NOT NULL DEFAULT 'specialist'"
                )
            # Синхронизируем role='director' для всех у кого profession=2 или is_director=1
            await c.execute(
                "UPDATE specialists SET role='director' WHERE (profession=2 OR is_director=1) AND role != 'director'"
            )
            await _maybe_await(conn.commit())

            # request_type
            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chats'
                   AND COLUMN_NAME = 'request_type'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chats ADD COLUMN request_type VARCHAR(128) NULL AFTER profession")

            # specialist_id -> NULLable (важно для общей очереди)
            await c.execute("""
                SELECT IS_NULLABLE
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chats'
                   AND COLUMN_NAME = 'specialist_id'
                 LIMIT 1
            """)
            row = await c.fetchone()
            if row and str(row.get("IS_NULLABLE") or "").upper() == "NO":
                await c.execute("ALTER TABLE support_chats MODIFY specialist_id BIGINT NULL")

            # accepted_at — время принятия заявки специалистом
            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chats'
                   AND COLUMN_NAME = 'accepted_at'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chats ADD COLUMN accepted_at TIMESTAMP NULL DEFAULT NULL AFTER updated_at")

            # closed_at — время закрытия чата
            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chats'
                   AND COLUMN_NAME = 'closed_at'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chats ADD COLUMN closed_at TIMESTAMP NULL DEFAULT NULL AFTER accepted_at")

            # Убрать кривой уникальный индекс, который ломает закрытие
            # (у тебя он давал Duplicate entry ... closed)
            if await _index_exists(c, "support_chats", "uq_active_chat"):
                try:
                    await c.execute("ALTER TABLE support_chats DROP INDEX uq_active_chat")
                except Exception:
                    pass

            # declines table
            await c.execute("""
                CREATE TABLE IF NOT EXISTS support_chat_declines (
                  chat_id BIGINT NOT NULL,
                  specialist_id BIGINT NOT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (chat_id, specialist_id),
                  KEY ix_decl_spec (specialist_id),
                  CONSTRAINT fk_decl_chat FOREIGN KEY (chat_id) REFERENCES support_chats(id) ON DELETE CASCADE,
                  CONSTRAINT fk_decl_spec FOREIGN KEY (specialist_id) REFERENCES specialists(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # payments table
            await c.execute("""
                CREATE TABLE IF NOT EXISTS support_chat_payments (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  chat_id BIGINT NOT NULL,
                  specialist_id BIGINT NOT NULL,
                  amount_rub INT NOT NULL,
                  payment_id VARCHAR(64) NOT NULL,
                  pay_url TEXT NOT NULL,
                  status VARCHAR(32) NOT NULL DEFAULT 'NEW',
                  bonus_points_applied INT NOT NULL DEFAULT 0,
                  bonus_discount_rub INT NOT NULL DEFAULT 0,
                  cash_amount_rub INT NOT NULL DEFAULT 0,
                  bonus_reservation_key VARCHAR(128) NULL,
                  payment_method VARCHAR(32) NOT NULL DEFAULT 'CASH_ONLY',
                  paid_at TIMESTAMP NULL DEFAULT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  UNIQUE KEY uq_payment_id (payment_id),
                  KEY ix_pay_chat (chat_id),
                  KEY ix_pay_chat_status (chat_id, status),
                  KEY ix_pay_paid_at (paid_at),
                  CONSTRAINT fk_pay_chat FOREIGN KEY (chat_id) REFERENCES support_chats(id) ON DELETE CASCADE,
                  CONSTRAINT fk_pay_spec FOREIGN KEY (specialist_id) REFERENCES specialists(id) ON DELETE RESTRICT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # Мягкая миграция колонок support_chat_payments для старых БД
            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chat_payments'
                   AND COLUMN_NAME = 'bonus_points_applied'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chat_payments ADD COLUMN bonus_points_applied INT NOT NULL DEFAULT 0 AFTER status")

            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chat_payments'
                   AND COLUMN_NAME = 'bonus_discount_rub'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chat_payments ADD COLUMN bonus_discount_rub INT NOT NULL DEFAULT 0 AFTER bonus_points_applied")

            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chat_payments'
                   AND COLUMN_NAME = 'cash_amount_rub'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chat_payments ADD COLUMN cash_amount_rub INT NOT NULL DEFAULT 0 AFTER bonus_discount_rub")

            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chat_payments'
                   AND COLUMN_NAME = 'bonus_reservation_key'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chat_payments ADD COLUMN bonus_reservation_key VARCHAR(128) NULL AFTER cash_amount_rub")

            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chat_payments'
                   AND COLUMN_NAME = 'payment_method'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chat_payments ADD COLUMN payment_method VARCHAR(32) NOT NULL DEFAULT 'CASH_ONLY' AFTER bonus_reservation_key")

            await c.execute("""
                SELECT COUNT(*) AS c
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'support_chat_payments'
                   AND COLUMN_NAME = 'paid_at'
            """)
            if int(((await c.fetchone()) or {}).get("c") or 0) == 0:
                await c.execute("ALTER TABLE support_chat_payments ADD COLUMN paid_at TIMESTAMP NULL DEFAULT NULL AFTER payment_method")

            if not await _index_exists(c, "support_chat_payments", "ix_pay_chat_status"):
                await c.execute("ALTER TABLE support_chat_payments ADD INDEX ix_pay_chat_status (chat_id, status)")
            if not await _index_exists(c, "support_chat_payments", "ix_pay_paid_at"):
                await c.execute("ALTER TABLE support_chat_payments ADD INDEX ix_pay_paid_at (paid_at)")

            await _maybe_await(conn.commit())

async def clear_chat_declines(chat_id: int):
    """
    Очищает отказы специалистов по заявке,
    чтобы она могла снова появиться в пуле.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "DELETE FROM support_chat_declines WHERE chat_id = %s",
                (chat_id,)
            )
            await _maybe_await(conn.commit())


async def create_support_chat_request(
    client_user_id: str | int | None = None,
    profession: int = 0,
    request_type: str | None = None,
    category: str | None = None,
    *,
    internal_user_id: int | str | None = None,
    tg_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
    ensure_user_exists: bool = True,
    source: str | None = None,
) -> int:
    """
    Создаём заявку (pending) БЕЗ назначенного специалиста.
    ВАЖНО: open_key — GENERATED, его НЕЛЬЗЯ вставлять/обновлять руками.
    В support_chats.client_user_id хранится только внутренний users.id.
    """
    resolved_client_user_id = await _resolve_chat_client_user_id(
        client_user_id=client_user_id,
        internal_user_id=internal_user_id,
        tg_user_id=tg_user_id,
        max_user_id=max_user_id,
        create=ensure_user_exists,
    )
    if not resolved_client_user_id:
        raise ValueError("Cannot resolve chat client to internal users.id")
    profession = int(profession)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO support_chats
                    (client_user_id, specialist_id, profession, request_type, category, status, source, created_at)
                VALUES
                    (%s, NULL, %s, %s, %s, 'pending', %s, NOW())
                """,
                (int(resolved_client_user_id), profession, request_type, category, source),
            )

            await _maybe_await(conn.commit())
            return int(c.lastrowid)

async def list_chats_for_director(*, profession: int | None = None) -> list[dict]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if profession is None:
                await c.execute("""
                    SELECT
                      ch.*,
                      COALESCE(NULLIF(u.first_name, ''), 'Клиент') AS client_name
                    FROM support_chats ch
                    LEFT JOIN users u ON u.id = ch.client_user_id
                    WHERE ch.status IN ('pending', 'active', 'consultation', 'test')
                      AND (ch.request_type IS NULL OR ch.request_type != 'order')
                    ORDER BY (ch.status='pending') DESC, ch.updated_at DESC, ch.id DESC
                """)
            else:
                await c.execute("""
                    SELECT
                      ch.*,
                      COALESCE(NULLIF(u.first_name, ''), 'Клиент') AS client_name
                    FROM support_chats ch
                    LEFT JOIN users u ON u.id = ch.client_user_id
                    WHERE ch.status IN ('pending', 'active', 'consultation', 'test')
                      AND (ch.request_type IS NULL OR ch.request_type != 'order')
                      AND ch.profession=%s
                    ORDER BY (ch.status='pending') DESC, ch.updated_at DESC, ch.id DESC
                """, (int(profession),))

            return await c.fetchall() or []

async def get_director_chat_counts() -> dict:
    """
    Счётчики заявок для меню директора.
    Считаем статусы pending/active/consultation.
    Возвращаем:
      {
        "all": {"pending": int, "active": int, "total": int},
        <profession_int>: {"pending": int, "active": int, "total": int},
      }
    """
    out: dict = {"all": {"pending": 0, "active": 0, "total": 0}}

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  profession,
                  SUM(status='pending')                 AS pending_cnt,
                  SUM(status='active' OR status='consultation') AS active_cnt,
                  COUNT(*)                              AS total_cnt
                FROM support_chats
                WHERE status IN ('pending','active','consultation','test')
                  AND (request_type IS NULL OR request_type != 'order')
                GROUP BY profession
                """
            )
            rows = await c.fetchall() or []

    # заполняем по профессиям (поддерживаем новые отделы автоматически)
    for r in rows:
        if r.get("profession") is None:
            continue
        prof = int(r["profession"])

        p = int(r.get("pending_cnt") or 0)
        a = int(r.get("active_cnt") or 0)
        t = int(r.get("total_cnt") or 0)

        out[prof] = {"pending": p, "active": a, "total": t}

        out["all"]["pending"] += p
        out["all"]["active"] += a
        out["all"]["total"] += t

    # гарантируем ключи для известных разделов (чтобы UI не падал)
    for prof in (0, 1, 2, 3):
        out.setdefault(prof, {"pending": 0, "active": 0, "total": 0})

    return out



async def get_director_specialist() -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("""
                SELECT *
                  FROM specialists
                 WHERE role='director'
                   AND is_active=1
                 ORDER BY id ASC
                 LIMIT 1
            """)
            return await c.fetchone()


PARTNER_DIRECTOR_REQUEST_TYPE = "partner_director"
PARTNER_DIRECTOR_CATEGORY = "Чат с директором"
_SPECIALISTS_LINKED_USER_SUPPORTED: bool | None = None


async def _ensure_specialists_linked_user_column(conn) -> bool:
    """Проверяет/создаёт specialists.linked_user_id (best-effort)."""
    global _SPECIALISTS_LINKED_USER_SUPPORTED
    if _SPECIALISTS_LINKED_USER_SUPPORTED is not None:
        return _SPECIALISTS_LINKED_USER_SUPPORTED

    async with await _cur(conn) as c:
        await c.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM information_schema.columns
             WHERE table_schema = DATABASE()
               AND table_name = 'specialists'
               AND column_name = 'linked_user_id'
            """
        )
        row = await c.fetchone() or {}
        if int(row.get("cnt") or 0) > 0:
            _SPECIALISTS_LINKED_USER_SUPPORTED = True
            return True

        try:
            await c.execute("ALTER TABLE specialists ADD COLUMN linked_user_id BIGINT NULL DEFAULT NULL")
            await _maybe_await(conn.commit())
            _SPECIALISTS_LINKED_USER_SUPPORTED = True
        except Exception:
            _SPECIALISTS_LINKED_USER_SUPPORTED = False
    return bool(_SPECIALISTS_LINKED_USER_SUPPORTED)


def _norm_partner_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


async def find_partner_specialist_for_user(
    *,
    partner_user_id: int | None = None,
    tg_user_id: int | None = None,
    max_user_id: int | None = None,
    phone: str | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Ищет partner-specialist (profession=5) для users-записи.
    Использует linked_user_id (если есть), tg/max id и телефон.
    """
    phone_n = _norm_partner_phone(phone)

    async with get_conn() as conn:
        has_linked = await _ensure_specialists_linked_user_column(conn)
        async with await _cur(conn) as c:
            if has_linked and partner_user_id:
                await c.execute(
                    """
                    SELECT * FROM specialists
                     WHERE profession=5
                       AND is_active=1
                       AND linked_user_id=%s
                     LIMIT 1
                    """,
                    (int(partner_user_id),),
                )
                row = await c.fetchone()
                if row:
                    return row

            if tg_user_id:
                await c.execute(
                    """
                    SELECT * FROM specialists
                     WHERE profession=5
                       AND is_active=1
                       AND tg_user_id=%s
                     LIMIT 1
                    """,
                    (int(tg_user_id),),
                )
                row = await c.fetchone()
                if row:
                    return row

            if max_user_id:
                await c.execute(
                    """
                    SELECT * FROM specialists
                     WHERE profession=5
                       AND is_active=1
                       AND max_user_id=%s
                     LIMIT 1
                    """,
                    (int(max_user_id),),
                )
                row = await c.fetchone()
                if row:
                    return row

            if phone_n:
                await c.execute(
                    """
                    SELECT *
                      FROM specialists
                     WHERE profession=5
                       AND is_active=1
                       AND RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'(',''),')',''),'-',''),' ',''), 10) = RIGHT(%s, 10)
                     LIMIT 1
                    """,
                    (phone_n,),
                )
                row = await c.fetchone()
                if row:
                    return row
    return None


async def ensure_partner_specialist_linked_user(
    *,
    partner_specialist_id: int,
    partner_user_id: int,
) -> None:
    """Привязывает specialists.linked_user_id к users.id партнёра (best-effort)."""
    async with get_conn() as conn:
        if not await _ensure_specialists_linked_user_column(conn):
            return
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE specialists
                   SET linked_user_id = COALESCE(linked_user_id, %s)
                 WHERE id=%s
                """,
                (int(partner_user_id), int(partner_specialist_id)),
            )
            await _maybe_await(conn.commit())




async def resolve_partner_identity_by_phone(
    *,
    internal_user_id: int | None = None,
    tg_user_id: int | None = None,
    max_user_id: int | None = None,
    phone: str | None = None,
) -> Dict[str, Any]:
    """
    Централизованно определяет партнёра (profession=5) и канонический users.id по телефону.
    """
    from db.users import resolve_internal_user_id

    resolved_internal_id = int(internal_user_id) if internal_user_id else None
    if not resolved_internal_id:
        resolved_internal_id = await resolve_internal_user_id(
            tg_user_id=int(tg_user_id) if tg_user_id else None,
            max_user_id=int(max_user_id) if max_user_id else None,
            create=False,
        )

    candidates: list[Dict[str, Any]] = []
    phone_n = _norm_partner_phone(phone)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if resolved_internal_id:
                await c.execute("SELECT id, phone, first_name, tg_user_id, max_user_id, created_at FROM users WHERE id=%s LIMIT 1", (int(resolved_internal_id),))
                u = await c.fetchone()
                if u:
                    candidates.append(u)
                    phone_n = phone_n or _norm_partner_phone(u.get('phone'))

            if tg_user_id:
                await c.execute("SELECT id, phone, first_name, tg_user_id, max_user_id, created_at FROM users WHERE tg_user_id=%s", (str(int(tg_user_id)),))
                candidates.extend(await c.fetchall() or [])
            if max_user_id:
                await c.execute("SELECT id, phone, first_name, tg_user_id, max_user_id, created_at FROM users WHERE max_user_id=%s", (str(int(max_user_id)),))
                candidates.extend(await c.fetchall() or [])

            if phone_n:
                await c.execute("""
                    SELECT id, phone, first_name, tg_user_id, max_user_id, created_at
                      FROM users
                     WHERE RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''), 10) = RIGHT(%s, 10)
                """, (phone_n,))
                candidates.extend(await c.fetchall() or [])

    dedup: dict[int, Dict[str, Any]] = {}
    for row in candidates:
        rid = int(row.get('id') or 0)
        if rid > 0:
            dedup[rid] = row
    users_list = list(dedup.values())

    if not phone_n and users_list:
        for u in users_list:
            phone_n = _norm_partner_phone(u.get('phone'))
            if phone_n:
                break

    partner_spec = await find_partner_specialist_for_user(
        partner_user_id=resolved_internal_id,
        tg_user_id=int(tg_user_id) if tg_user_id else None,
        max_user_id=int(max_user_id) if max_user_id else None,
        phone=phone_n,
    )
    if not partner_spec:
        return {"partner_detected": False, "canonical_user_id": resolved_internal_id, "current_phone": phone_n}

    partner_spec_id = int(partner_spec.get('id') or 0)
    linked_user_id = int(partner_spec.get('linked_user_id') or 0) if partner_spec.get('linked_user_id') else 0
    canonical_user_id = linked_user_id if linked_user_id > 0 else None

    if not canonical_user_id and users_list:
        users_sorted = sorted(users_list, key=lambda x: int(x.get('id') or 0))
        canonical_user_id = int(users_sorted[0]['id'])

    if not canonical_user_id:
        canonical_user_id = resolved_internal_id

    canonical_user = None
    for u in users_list:
        if int(u.get('id') or 0) == int(canonical_user_id or 0):
            canonical_user = u
            break

    return {
        "partner_detected": bool(partner_spec_id and canonical_user_id),
        "partner_specialist_id": partner_spec_id or None,
        "canonical_user_id": int(canonical_user_id) if canonical_user_id else None,
        "partner_phone": _norm_partner_phone((canonical_user or {}).get('phone') or partner_spec.get('phone') or phone_n),
        "partner_name": ((canonical_user or {}).get('first_name') or partner_spec.get('name') or '').strip() or None,
        "current_phone": phone_n,
    }
async def ensure_partner_director_chat(
    partner_specialist_id: int,
    partner_user_id: int,
    source: str | None = None,
) -> int:
    """Идемпотентно гарантирует постоянный активный чат партнёра с директором."""
    partner_specialist_id = int(partner_specialist_id)
    partner_user_id = int(partner_user_id)

    director = await get_director_specialist()
    if not director:
        raise ValueError("Director specialist not found")
    director_id = int(director.get("id") or 0)
    if director_id <= 0:
        raise ValueError("Director specialist id is invalid")

    director_profession = int(director.get("profession") or 2)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, status, request_type, category
                  FROM support_chats
                 WHERE client_user_id=%s
                   AND specialist_id=%s
                   AND status IN ('pending','active','consultation')
                   AND (
                        request_type=%s
                        OR category=%s
                   )
                 ORDER BY id DESC
                """,
                (partner_user_id, director_id, PARTNER_DIRECTOR_REQUEST_TYPE, PARTNER_DIRECTOR_CATEGORY),
            )
            rows = await c.fetchall() or []
            if rows:
                head = rows[0]
                chat_id = int(head["id"])
                need_activate = str(head.get("status") or "").lower() != "active"
                need_normalize = (
                    str(head.get("request_type") or "").strip().lower() != PARTNER_DIRECTOR_REQUEST_TYPE
                    or str(head.get("category") or "").strip() != PARTNER_DIRECTOR_CATEGORY
                )
                if need_activate or need_normalize:
                    await c.execute(
                        """
                        UPDATE support_chats
                           SET status='active',
                               request_type=%s,
                               category=%s,
                               updated_at=CURRENT_TIMESTAMP
                         WHERE id=%s
                        """,
                        (PARTNER_DIRECTOR_REQUEST_TYPE, PARTNER_DIRECTOR_CATEGORY, chat_id),
                    )

                # На всякий случай закрываем дубли активных партнёрских чатов для этого партнёра.
                duplicate_ids = [int(r["id"]) for r in rows[1:] if int(r.get("id") or 0) > 0]
                if duplicate_ids:
                    placeholders = ",".join(["%s"] * len(duplicate_ids))
                    await c.execute(
                        f"""
                        UPDATE support_chats
                           SET status='closed',
                               closed_at=COALESCE(closed_at, CURRENT_TIMESTAMP),
                               updated_at=CURRENT_TIMESTAMP
                         WHERE id IN ({placeholders})
                        """,
                        tuple(duplicate_ids),
                    )

                if need_activate or need_normalize or duplicate_ids:
                    await _maybe_await(conn.commit())
                return chat_id

            await c.execute(
                """
                INSERT INTO support_chats
                    (client_user_id, specialist_id, profession, request_type, category, status, source, created_at)
                VALUES
                    (%s, %s, %s, %s, %s, 'active', %s, NOW())
                """,
                (
                    partner_user_id,
                    director_id,
                    director_profession,
                    PARTNER_DIRECTOR_REQUEST_TYPE,
                    PARTNER_DIRECTOR_CATEGORY,
                    source,
                ),
            )
            await _maybe_await(conn.commit())
            return int(c.lastrowid)


async def ensure_partner_entrypoint_chat(
    *,
    partner_user_id: int | None = None,
    tg_user_id: int | None = None,
    max_user_id: int | None = None,
    phone: str | None = None,
    source: str | None = None,
) -> int | None:
    """
    Объединённый helper:
    - находит partner-specialist (profession=5);
    - связывает specialists.linked_user_id с users.id;
    - гарантирует постоянный чат с директором.
    """
    identity = await resolve_partner_identity_by_phone(
        internal_user_id=partner_user_id,
        tg_user_id=int(tg_user_id) if tg_user_id else None,
        max_user_id=int(max_user_id) if max_user_id else None,
        phone=phone,
    )
    if not identity.get("partner_detected"):
        return None

    canonical_user_id = int(identity.get("canonical_user_id") or 0)
    partner_spec_id = int(identity.get("partner_specialist_id") or 0)
    if canonical_user_id <= 0 or partner_spec_id <= 0:
        return None

    await ensure_partner_specialist_linked_user(
        partner_specialist_id=partner_spec_id,
        partner_user_id=canonical_user_id,
    )

    return await ensure_partner_director_chat(
        partner_specialist_id=partner_spec_id,
        partner_user_id=canonical_user_id,
        source=source,
    )


async def list_chats_for_specialist(specialist_id: int) -> list[dict]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:

            await c.execute("SELECT profession FROM specialists WHERE id=%s LIMIT 1", (int(specialist_id),))
            row = await c.fetchone()
            if not row:
                return []
            prof = int(row.get("profession") or 0)

            await c.execute(
                """
                SELECT
                  ch.*,
                  COALESCE(NULLIF(u.first_name, ''), 'Клиент') AS client_name
                FROM support_chats ch
                LEFT JOIN users u ON u.id = ch.client_user_id
                WHERE
                  (ch.request_type IS NULL OR ch.request_type != 'order')
                  AND (
                    (ch.status='pending' AND ch.profession=%s AND ch.specialist_id IS NULL)
                    OR
                    (ch.status IN ('active','pending','consultation','test') AND ch.specialist_id=%s)
                  )
                ORDER BY (ch.status='pending') DESC, ch.updated_at DESC, ch.id DESC
                """,
                (prof, int(specialist_id)),
            )
            return await c.fetchall() or []

async def list_specialists_for_profession(profession: int) -> List[Dict[str, Any]]:
    """
    Для рассылки/пушей и общего списка:
    - profession совпадает
    - is_active=1
    - phone_verified=1 (ОБЯЗАТЕЛЬНО как ты хочешь)
    tg_user_id НЕ делаем обязательным, потому что:
      - пуш уйдёт только тем, у кого tg_user_id есть
      - остальные увидят pending-заявку в кабинете специалиста
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT *
                  FROM specialists
                 WHERE profession=%s
                   AND is_active=1
                   AND profession<>5
                   AND phone_verified=1
                 ORDER BY id ASC
                """,
                (int(profession),),
            )
            return await c.fetchall() or []


async def count_active_specialists_for_profession(profession: int) -> int:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COUNT(*) AS c
                  FROM specialists
                 WHERE profession=%s AND is_active=1 AND phone_verified=1
                """,
                (int(profession),),
            )
            row = await c.fetchone() or {}
            return int(row.get("c") or 0)


async def try_accept_chat(chat_id: int, specialist_id: int) -> bool:
    """
    Принять заявку: только если она pending и ещё никем не назначена.
    Делает её active и назначает specialist_id.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE support_chats
                   SET specialist_id=%s,
                       status='active',
                       updated_at=CURRENT_TIMESTAMP,
                       accepted_at=COALESCE(accepted_at, CURRENT_TIMESTAMP)
                 WHERE id=%s
                   AND status='pending'
                   AND specialist_id IS NULL
                """,
                (int(specialist_id), int(chat_id)),
            )
            await _maybe_await(conn.commit())
            return c.rowcount == 1


async def force_assign_chat(chat_id: int, specialist_id: int, consultation: bool = False) -> bool:
    """
    Принудительно назначить, если заявка всё ещё pending и без спеца.
    """
    if consultation:
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    """
                    UPDATE support_chats
                       SET specialist_id=%s,
                           status='consultation',
                           updated_at=CURRENT_TIMESTAMP,
                           accepted_at=COALESCE(accepted_at, CURRENT_TIMESTAMP)
                     WHERE id=%s
                       AND status='pending'
                       AND specialist_id IS NULL
                    """,
                    (int(specialist_id), int(chat_id)),
                )
                await _maybe_await(conn.commit())
                return c.rowcount == 1

    return await try_accept_chat(chat_id, specialist_id)


async def mark_chat_declined(chat_id: int, specialist_id: int) -> None:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            try:
                await c.execute(
                    "INSERT INTO support_chat_declines (chat_id, specialist_id) VALUES (%s,%s)",
                    (int(chat_id), int(specialist_id)),
                )
                await _maybe_await(conn.commit())
            except IntegrityError:
                # уже отказался ранее
                pass


async def count_declines_for_chat(chat_id: int) -> int:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT COUNT(*) AS c FROM support_chat_declines WHERE chat_id=%s", (int(chat_id),))
            row = await c.fetchone() or {}
            return int(row.get("c") or 0)


async def get_least_busy_specialist_for_profession(profession: int) -> Optional[Dict[str, Any]]:
    """
    Если все отказались — выбираем наименее занятого (по active/pending назначенным на него чатам).
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT s.*,
                       COALESCE(cnt.cnt, 0) AS active_clients
                  FROM specialists s
                  LEFT JOIN (
                    SELECT specialist_id, COUNT(*) AS cnt
                      FROM support_chats
                     WHERE status IN ('pending','active')
                       AND specialist_id IS NOT NULL
                     GROUP BY specialist_id
                  ) cnt ON cnt.specialist_id = s.id
                 WHERE s.profession=%s
                   AND s.is_active=1
                   AND s.profession<>5
                   AND s.phone_verified=1
                 ORDER BY active_clients ASC, s.id ASC
                 LIMIT 1
                """,
                (int(profession),),
            )
            return await c.fetchone()


async def list_ready_specialists_for_notify() -> List[Dict[str, Any]]:
    """
    Все специалисты, которым можно слать уведомления:
    - активны
    - телефон подтвержден
    - tg_user_id привязан
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, tg_user_id, profession, role
                  FROM specialists
                 WHERE is_active=1
                   AND phone_verified=1
                   AND tg_user_id IS NOT NULL
                   AND profession<>5
                """
            )
            return await c.fetchall() or []


async def list_pending_chat_ids_for_profession(profession: int) -> List[int]:
    """
    Pending-очередь по профессии (еще НЕ назначено specialist_id).
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id
                  FROM support_chats
                 WHERE status='pending'
                   AND profession=%s
                   AND specialist_id IS NULL
                 ORDER BY updated_at DESC, id DESC
                """,
                (int(profession),),
            )
            rows = await c.fetchall() or []
            return [int(r["id"]) for r in rows if r and r.get("id") is not None]


async def count_pending_for_profession(profession: int) -> int:
    """
    Кол-во pending в общей очереди (по профессии).
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT COUNT(*) AS c
                  FROM support_chats
                 WHERE status='pending'
                   AND profession=%s
                   AND specialist_id IS NULL
                """,
                (int(profession),),
            )
            row = await c.fetchone() or {}
            return int(row.get("c") or 0)

async def count_pending_unassigned(*, profession: int | None = None) -> int:
    """
    Сколько непринятых заявок (status='pending' и specialist_id IS NULL).
    Если profession=None — считаем по всем отделам (для директора).
    Если profession задан — только по отделу (для бухгалтера/спеца).
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if profession is None:
                await c.execute(
                    """
                    SELECT COUNT(*) AS c
                      FROM support_chats
                     WHERE status='pending'
                       AND specialist_id IS NULL
                    """
                )
            else:
                await c.execute(
                    """
                    SELECT COUNT(*) AS c
                      FROM support_chats
                     WHERE status='pending'
                       AND specialist_id IS NULL
                       AND profession=%s
                    """,
                    (int(profession),),
                )
            row = await c.fetchone() or {}
            return int(row.get("c") or 0)

async def ensure_specialist_max_column() -> None:
    """Добавляет колонки max_user_id и max_username в specialists, если их ещё нет."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # max_user_id
            await c.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM information_schema.columns
                 WHERE table_schema=DATABASE()
                   AND table_name='specialists'
                   AND column_name='max_user_id'
                """
            )
            row = await c.fetchone() or {}
            if int(row.get("cnt") or 0) == 0:
                await c.execute(
                    "ALTER TABLE specialists ADD COLUMN max_user_id BIGINT NULL DEFAULT NULL"
                )
                await _maybe_await(conn.commit())
            # max_username
            await c.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM information_schema.columns
                 WHERE table_schema=DATABASE()
                   AND table_name='specialists'
                   AND column_name='max_username'
                """
            )
            row2 = await c.fetchone() or {}
            if int(row2.get("cnt") or 0) == 0:
                await c.execute(
                    "ALTER TABLE specialists ADD COLUMN max_username VARCHAR(64) NULL DEFAULT NULL"
                )
                await _maybe_await(conn.commit())


async def get_specialist_by_max(*, max_user_id: int | None, max_username: str | None = None) -> Optional[Dict[str, Any]]:
    """
    Ищем специалиста по max_user_id (приоритет) или по tg_username/max_username (fallback).
    Аналог get_specialist_by_tg для MAX-мессенджера.
    """
    max_username = (max_username or "").strip().lstrip("@") or None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # 1) приоритетно по max_user_id
            if max_user_id:
                await c.execute(
                    "SELECT * FROM specialists WHERE max_user_id=%s AND is_active=1 LIMIT 1",
                    (int(max_user_id),),
                )
                row = await c.fetchone()
                if row:
                    return row

            # 2) fallback по max_username или tg_username
            if max_username:
                await c.execute(
                    """SELECT * FROM specialists
                       WHERE (LOWER(max_username)=%s OR LOWER(tg_username)=%s)
                         AND is_active=1 LIMIT 1""",
                    (max_username.lower(), max_username.lower()),
                )
                row = await c.fetchone()
                if row:
                    # автопривязка max_user_id
                    if max_user_id and not row.get("max_user_id"):
                        await c.execute(
                            "UPDATE specialists SET max_user_id=%s WHERE id=%s",
                            (int(max_user_id), int(row["id"])),
                        )
                        await _maybe_await(conn.commit())
                        row["max_user_id"] = max_user_id
                    return row

            return None


async def get_specialist_by_phone(phone: str | None) -> Optional[Dict[str, Any]]:
    """Ищем активного специалиста по номеру телефона (с нормализацией до 11 цифр)."""
    raw = str(phone or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not (len(digits) == 11 and digits.startswith("7")):
        return None

    variants = [
        digits,
        f"+{digits}",
        f"8{digits[1:]}",
        f"7{digits[1:3]}{digits[3:6]}{digits[6:8]}{digits[8:]}",
        f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}",
    ]
    seen = set()
    uniq_variants = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            uniq_variants.append(v)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            placeholders = ",".join(["%s"] * len(uniq_variants))
            await c.execute(
                f"""SELECT * FROM specialists
                       WHERE is_active=1
                         AND phone IN ({placeholders})
                       LIMIT 1""",
                tuple(uniq_variants),
            )
            row = await c.fetchone()
            if row:
                return row

            await c.execute(
                """SELECT * FROM specialists
                       WHERE is_active=1
                         AND REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'(',''),')',''),'-',''),' ','')=%s
                       LIMIT 1""",
                (digits,),
            )
            return await c.fetchone()


async def link_specialist_max_user(specialist_id: int, max_user_id: int, username: str | None = None) -> None:
    """Привязать MAX user_id и max_username к специалисту."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if username:
                await c.execute(
                    "UPDATE specialists SET max_user_id=%s, max_username=%s WHERE id=%s",
                    (int(max_user_id), username.lstrip("@").strip(), int(specialist_id)),
                )
            else:
                await c.execute(
                    "UPDATE specialists SET max_user_id=%s WHERE id=%s",
                    (int(max_user_id), int(specialist_id)),
                )
            await _maybe_await(conn.commit())


async def list_ready_specialists_for_notify_max() -> List[Dict[str, Any]]:
    """Специалисты с max_user_id для уведомлений."""
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, max_user_id, profession, role
                  FROM specialists
                 WHERE is_active=1
                   AND phone_verified=1
                   AND max_user_id IS NOT NULL
                   AND profession<>5
                """
            )
            return await c.fetchall() or []


async def count_consultations(*, profession: int | None = None) -> int:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if profession is None:
                await c.execute("""
                    SELECT COUNT(*) AS c
                      FROM support_chats
                     WHERE status='consultation'
                """)
            else:
                await c.execute("""
                    SELECT COUNT(*) AS c
                      FROM support_chats
                     WHERE status='consultation'
                       AND profession=%s
                """, (int(profession),))
            row = await c.fetchone() or {}
            return int(row.get("c") or 0)


###CRUD для файлов
async def add_chat_file(
    chat_id: int,
    sender: int,
    original_name: str,
    stored_name: str,
    mime: str,
    size_bytes: int,
    message_id: Optional[int] = None,
) -> int:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO support_chat_files
                  (chat_id, message_id, sender, original_name, stored_name, mime, size_bytes)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(chat_id),
                    int(message_id) if message_id is not None else None,
                    int(sender),
                    original_name,
                    stored_name,
                    mime,
                    int(size_bytes),
                ),
            )
            await _maybe_await(conn.commit())
            return int(c.lastrowid)

async def attach_file_to_message(file_id: int, message_id: int) -> bool:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "UPDATE support_chat_files SET message_id=%s WHERE id=%s",
                (int(message_id), int(file_id)),
            )
            await _maybe_await(conn.commit())
            return (c.rowcount or 0) > 0

async def get_chat_file_by_id(file_id: int) -> Optional[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, chat_id, message_id, sender, original_name, stored_name, mime, size_bytes, created_at
                  FROM support_chat_files
                 WHERE id=%s
                 LIMIT 1
                """,
                (int(file_id),),
            )
            return await c.fetchone()

async def get_chat_files_after(chat_id: int, after_id: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, chat_id, message_id, sender, original_name, stored_name, mime, size_bytes, created_at
                  FROM support_chat_files
                 WHERE chat_id=%s AND id > %s
                 ORDER BY id ASC
                 LIMIT %s
                """,
                (int(chat_id), int(after_id), int(limit)),
            )
            return await c.fetchall() or []

async def get_files_by_message_id(message_id: int) -> List[Dict[str, Any]]:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT id, chat_id, message_id, sender, original_name, stored_name, mime, size_bytes, created_at
                  FROM support_chat_files
                 WHERE message_id=%s
                 ORDER BY id ASC
                """,
                (int(message_id),),
            )
            return await c.fetchall() or []

async def delete_chat_file(file_id: int) -> bool:
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("DELETE FROM support_chat_files WHERE id=%s", (int(file_id),))
            await _maybe_await(conn.commit())
            return (c.rowcount or 0) > 0

###секретные ключи


async def resolve_chat_access_token(token: str, ttl_min: int = TOKEN_TTL_MIN, max_hops: int = 20) -> str | None:
    """
    Делает токен "живым" для открытия чата из старой кнопки.

    Правила:
    - если токен отозван, но есть replaced_by -> переходим на новый (по цепочке)
    - если токен истёк -> делаем refresh и возвращаем свежий
    - refresh разрешён даже спустя дни/недели (TOKEN_MAX_STALE_DAYS)
    """
    token = (token or "").strip()
    if not token:
        return None

    cur_token = token
    for _ in range(int(max_hops)):
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute(
                    """
                    SELECT id, token, chat_id, role, actor_id, expires_at, revoked_at, replaced_by
                      FROM support_chat_tokens
                     WHERE token=%s
                     LIMIT 1
                    """,
                    (cur_token,),
                )
                row = await c.fetchone()

        if not row:
            return None

        # если уже отозван и есть замена — идём дальше по цепочке
        if row.get("revoked_at") and row.get("replaced_by"):
            rep_id = int(row["replaced_by"])
            async with get_conn() as conn:
                async with await _cur(conn) as c:
                    await c.execute(
                        "SELECT token FROM support_chat_tokens WHERE id=%s LIMIT 1",
                        (rep_id,),
                    )
                    rep = await c.fetchone()
            if not rep or not rep.get("token"):
                return None
            cur_token = str(rep["token"])
            continue

        # отозван без замены — нельзя
        if row.get("revoked_at") and not row.get("replaced_by"):
            return None

        expires_at = _parse_dt_maybe(row.get("expires_at"))
        now = datetime.utcnow()

        if expires_at and expires_at < now:
            # слишком старый токен не оживляем
            if expires_at < (now - timedelta(days=int(TOKEN_MAX_STALE_DAYS))):
                return None
            return await refresh_chat_access_token(cur_token, ttl_min=ttl_min)

        # токен активен
        return cur_token

    return None


async def create_chat_access_token(chat_id: int, role: str, actor_id: str | None = None, ttl_min: int = TOKEN_TTL_MIN) -> str:
    token = secrets.token_urlsafe(32)  # ~43+ символа, нормально
    expires = datetime.utcnow() + timedelta(minutes=int(ttl_min))

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO support_chat_tokens (token, chat_id, role, actor_id, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (token, int(chat_id), str(role), actor_id, expires),
            )
            await conn.commit()
    return token

async def get_chat_by_token(token: str):
    token = (token or "").strip()
    if not token:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT t.id as token_id, t.chat_id, t.role, t.actor_id, t.expires_at, t.revoked_at,
                       ch.*
                FROM support_chat_tokens t
                JOIN support_chats ch ON ch.id = t.chat_id
                WHERE t.token = %s
                LIMIT 1
                """,
                (token,),
            )
            rows = await c.fetchall() or []
            return rows[0] if rows else None

async def refresh_chat_access_token(old_token: str, ttl_min: int = TOKEN_TTL_MIN) -> str | None:
    """
    Создаёт новый токен на основе старого.
    - Если старый уже отозван и replaced_by заполнен — возвращаем актуальный токен (или его обновлённую версию).
    - Если старый истёк давно (старше TOKEN_MAX_STALE_DAYS) — не обновляем.
    - Используем SELECT ... FOR UPDATE для защиты от гонок (несколько refresh одновременно).
    """
    old_token = (old_token or "").strip()
    if not old_token:
        return None

    now = datetime.utcnow()
    new_token = secrets.token_urlsafe(32)
    new_expires = now + timedelta(minutes=int(ttl_min))

    async with get_conn() as conn:
        try:
            await _begin_tx(conn)
            async with await _cur(conn) as c:
                # берём старый с блокировкой строки
                await c.execute(
                    """
                    SELECT id, chat_id, role, actor_id, expires_at, revoked_at, replaced_by
                      FROM support_chat_tokens
                     WHERE token=%s
                     LIMIT 1
                     FOR UPDATE
                    """,
                    (old_token,),
                )
                row = await c.fetchone()
                if not row:
                    await _maybe_await(conn.rollback())
                    return None

                # если уже отозван — отдаём replaced_by (если есть)
                if row.get("revoked_at"):
                    rep_id = row.get("replaced_by")
                    await _maybe_await(conn.commit())
                    if rep_id:
                        # возвращаем токен замены; если он уже истёк — оживим его
                        async with get_conn() as conn2:
                            async with await _cur(conn2) as c2:
                                await c2.execute(
                                    """
                                    SELECT token, expires_at
                                      FROM support_chat_tokens
                                     WHERE id=%s
                                     LIMIT 1
                                    """,
                                    (int(rep_id),),
                                )
                                rep = await c2.fetchone()
                        if not rep or not rep.get("token"):
                            return None
                        rep_token = str(rep["token"])
                        rep_exp = _parse_dt_maybe(rep.get("expires_at"))
                        if rep_exp and rep_exp < now:
                            return await refresh_chat_access_token(rep_token, ttl_min=ttl_min)
                        return rep_token
                    return None

                expires_at = _parse_dt_maybe(row.get("expires_at"))

                # слишком старый токен — не оживляем
                if expires_at and expires_at < (now - timedelta(days=int(TOKEN_MAX_STALE_DAYS))):
                    await _maybe_await(conn.rollback())
                    return None

                chat_id = int(row["chat_id"])
                role = str(row["role"])
                actor_id = row.get("actor_id")

                # создаём новый
                await c.execute(
                    """
                    INSERT INTO support_chat_tokens (token, chat_id, role, actor_id, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (new_token, chat_id, role, actor_id, new_expires),
                )
                new_id = c.lastrowid

                # старый помечаем как заменённый
                await c.execute(
                    """
                    UPDATE support_chat_tokens
                       SET revoked_at=%s, replaced_by=%s
                     WHERE id=%s
                    """,
                    (now, int(new_id), int(row["id"])),
                )
                await _maybe_await(conn.commit())
                return new_token
        except Exception:
            try:
                await _maybe_await(conn.rollback())
            except Exception:
                pass
            raise


async def get_stale_agent_close_requests(hours: int = 48) -> List[Dict[str, Any]]:
    """
    Возвращает чаты, в которых специалист запросил закрытие, но клиент не ответил
    в течение `hours` часов. Используется для автоматического закрытия.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                    sc.id,
                    sc.category,
                    sc.status,
                    sc.client_user_id,
                    sc.specialist_id,
                    sc.close_req_agent_at,
                    u.tg_user_id  AS client_tg_id,
                    u.max_user_id AS client_max_id,
                    s.tg_user_id  AS spec_tg_id
                FROM support_chats sc
                LEFT JOIN users u ON u.id = sc.client_user_id
                LEFT JOIN specialists s ON s.id = sc.specialist_id
                WHERE sc.close_req_agent = 1
                  AND sc.close_req_client = 0
                  AND sc.status NOT IN ('closed')
                  AND sc.close_req_agent_at <= NOW() - INTERVAL %s HOUR
                """,
                (int(hours),),
            )
            return await c.fetchall() or []


async def auto_close_chat(chat_id: int) -> bool:
    """
    Принудительно закрывает чат (для автозакрытия по таймауту).
    Возвращает True если закрытие произошло, False если чат уже закрыт.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT status FROM support_chats WHERE id=%s FOR UPDATE",
                (int(chat_id),),
            )
            row = await c.fetchone() or {}
            if not row or str(row.get("status") or "") == "closed":
                await _maybe_await(conn.rollback())
                return False
            await c.execute(
                """
                UPDATE support_chats
                SET status='closed',
                    closed_at=COALESCE(closed_at, NOW()),
                    updated_at=NOW()
                WHERE id=%s
                """,
                (int(chat_id),),
            )
            await _maybe_await(conn.commit())
            return True


