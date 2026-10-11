from __future__ import annotations
import hashlib
import re
from typing import Any, Dict, List, Optional
from db.connection import (
    get_conn, _cur, _maybe_await, _begin_tx, _rollback_if_needed,
    _norm_inn, _norm_phone, _phone_last10,
    IntegrityError,
)


# =========================================================
# Users
# =========================================================

import logging as _logging

_log = _logging.getLogger(__name__)


async def _try_award_profile_field(internal_user_id: int, field_name: str) -> None:
    """Best-effort начисление бонуса за заполнение поля профиля."""
    try:
        from services.loyalty_engine import award_profile_completion_bonus
        await award_profile_completion_bonus(internal_user_id, field_name)
    except Exception:
        _log.debug("Loyalty profile bonus failed for user=%s field=%s", internal_user_id, field_name, exc_info=True)

async def _get_user_id_by_tg(tg_user_id: int | str) -> int | None:
    """Получить внутренний id пользователя по tg_user_id."""
    try:
        tg_uid = int(tg_user_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT id FROM users WHERE tg_user_id=%s LIMIT 1", (tg_uid,))
            row = await c.fetchone()
            return int(row["id"]) if row else None

async def _get_user_tg_id(user_id: int | str) -> int | None:
    """Получить внутренний id пользователя по tg_user_id."""
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT tg_user_id FROM users WHERE id=%s LIMIT 1", (user_id,))
            row = await c.fetchone()
            return int(row["tg_user_id"]) if row else None


async def find_existing_user_for_link(*, phone: str | None = None, inn: str | None = None) -> int | None:
    """Ищет существующего users.id для безопасной связки по телефону/ИНН."""
    phone_n = _norm_phone(phone) if phone else None
    inn_n = _norm_inn(inn) if inn else None
    if not phone_n and not inn_n:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            if phone_n:
                await c.execute("SELECT id FROM users WHERE phone=%s ORDER BY id ASC LIMIT 1", (phone_n,))
                row = await c.fetchone()
                if row:
                    return int(row["id"])
            if inn_n:
                await c.execute("SELECT id FROM users WHERE inn=%s ORDER BY id ASC LIMIT 1", (inn_n,))
                row = await c.fetchone()
                if row:
                    return int(row["id"])
    return None


async def resolve_internal_user_id(
    *,
    internal_user_id: int | str | None = None,
    tg_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
    create: bool = False,
) -> int | None:
    """
    Универсально резолвит внутренний users.id.

    Приоритет:
    1) internal_user_id
    2) tg_user_id
    3) max_user_id

    Если create=True, для tg/max разрешено создание записи пользователя.
    """
    provided = [
        value is not None and str(value).strip() != ""
        for value in (internal_user_id, tg_user_id, max_user_id)
    ]
    if sum(provided) == 0:
        return None

    if internal_user_id is not None and str(internal_user_id).strip() != "":
        try:
            uid = int(internal_user_id)
        except (ValueError, TypeError):
            return None
        async with get_conn() as conn:
            async with await _cur(conn) as c:
                await c.execute("SELECT id FROM users WHERE id=%s LIMIT 1", (uid,))
                row = await c.fetchone()
                return int(row["id"]) if row else None

    if tg_user_id is not None and str(tg_user_id).strip() != "":
        uid = await (_get_or_create_user_id(tg_user_id) if create else _get_user_id_by_tg(tg_user_id))
        return int(uid) if uid else None

    if max_user_id is not None and str(max_user_id).strip() != "":
        uid = await (_get_or_create_max_user_id(max_user_id) if create else _get_user_id_by_max(max_user_id))
        return int(uid) if uid else None

    return None


async def _get_or_create_user_id(tg_user_id: int | str) -> int:
    """Получить или создать внутренний id пользователя по tg_user_id.

    Важно: это вызывается из аналитики/логирования и может прилетать параллельно,
    поэтому делаем атомарный UPSERT без гонки вида SELECT -> INSERT.
    """
    try:
        tg_uid = int(tg_user_id)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid tg_user_id: {tg_user_id}")

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # LAST_INSERT_ID(id) позволяет получить id существующей строки через cursor.lastrowid
            await c.execute(
                """
                INSERT INTO users (tg_user_id, we_added, created_at, updated_at)
                VALUES (%s, 0, NOW(), NOW())
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    updated_at = NOW()
                """,
                (tg_uid,),
            )
            await _maybe_await(conn.commit())
            rid = int(getattr(c, "lastrowid", 0) or 0)
            if rid:
                return rid

    # крайне маловероятный fallback
    existing = await _get_user_id_by_tg(tg_uid)
    return int(existing or 0)


async def _resolve_public_user_id(
    user_id: str | int | None = None,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
    create: bool = False,
) -> int | None:
    return await resolve_internal_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=user_id,
        max_user_id=max_user_id,
        create=create,
    )


async def _get_user_row_by_any_id(
    user_id: str | int | None = None,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> Optional[Dict[str, Any]]:
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT * FROM users WHERE id=%s LIMIT 1", (int(uid),))
            row = await c.fetchone()
            if row:
                row["user_id"] = str(row.get("id") or "")
            return row


async def upsert_user(
    user_id: str,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    tg_username: str | None = None,
) -> None:
    """
    Создаёт или обновляет пользователя.
    user_id здесь - это telegram user_id (для обратной совместимости).
    В новой схеме он хранится в tg_user_id.
    """
    try:
        tg_uid = int(user_id)
    except (ValueError, TypeError):
        return
    phone_n = _norm_phone(phone) if phone else None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            linked_id = await find_existing_user_for_link(phone=phone_n)
            if linked_id:
                await c.execute("SELECT tg_user_id FROM users WHERE id=%s LIMIT 1", (int(linked_id),))
                existing = await c.fetchone() or {}
                if not existing.get("tg_user_id"):
                    await c.execute(
                        """
                        UPDATE users
                        SET tg_user_id=%s,
                            first_name = COALESCE(%s, first_name),
                            last_name = COALESCE(%s, last_name),
                            phone = COALESCE(%s, phone),
                            email = COALESCE(%s, email),
                            tg_username = COALESCE(%s, tg_username),
                            updated_at = NOW()
                        WHERE id=%s
                        """,
                        (tg_uid, first_name, last_name, phone_n, email, tg_username, int(linked_id)),
                    )
                    if phone_n is not None:
                        await _sync_user_verified_flag_by_id(c, int(linked_id))
                    await _maybe_await(conn.commit())
                    return
            await c.execute(
                """
                INSERT INTO users (tg_user_id, first_name, last_name, phone, email, tg_username, we_added, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s, 0, NOW(), NOW())
                ON DUPLICATE KEY UPDATE
                  first_name  = COALESCE(VALUES(first_name),  first_name),
                  last_name   = COALESCE(VALUES(last_name),   last_name),
                  phone       = COALESCE(VALUES(phone),       phone),
                  email       = COALESCE(VALUES(email),       email),
                  tg_username = COALESCE(VALUES(tg_username), tg_username),
                  updated_at  = NOW()
                """,
                (tg_uid, first_name, last_name, phone_n, email, tg_username),
            )

            # Если обновляли телефон, синхронизируем флаг верификации:
            # is_verified=1 только при наличии валидных ИНН + телефона.
            if phone is not None:
                await _sync_user_verified_flag(c, tg_uid)

            await _maybe_await(conn.commit())

    # Loyalty: бонус за заполнение полей профиля
    internal_id = await resolve_internal_user_id(tg_user_id=tg_uid)
    if internal_id:
        if phone:
            await _try_award_profile_field(internal_id, "phone")
        if email:
            await _try_award_profile_field(internal_id, "email")


async def _sync_user_verified_flag(c, tg_uid: int) -> bool:
    """
    Синхронизирует users.is_verified с фактическими данными.
    Возвращает итоговое значение флага.
    """
    await c.execute(
        "SELECT inn, phone FROM users WHERE tg_user_id=%s LIMIT 1",
        (tg_uid,),
    )
    row = await c.fetchone() or {}
    verified = bool(_norm_inn(row.get("inn")) and _norm_phone(row.get("phone")))

    await c.execute(
        "UPDATE users SET is_verified=%s, updated_at=NOW() WHERE tg_user_id=%s",
        (int(verified), tg_uid),
    )
    return verified


async def get_user(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Получить пользователя по telegram user_id.
    Возвращает dict с полем user_id (=tg_user_id как str) для обратной совместимости.
    """
    return await _get_user_row_by_any_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )


async def get_user_inn(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> Optional[str]:
    row = await _get_user_row_by_any_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    return (row.get("inn") if row else None) or None

async def create_user_ref_code(
    tg_user_id: str = "",
    *,
    internal_id: int = 0,
    max_user_id: int | str = "",
):
    """Создать реферальный код. Можно передать tg_user_id, max_user_id или internal_id."""
    uid = await _resolve_public_user_id(
        tg_user_id or None,
        internal_user_id=internal_id or None,
        max_user_id=max_user_id or None,
    )
    if not uid:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT * FROM users WHERE id=%s LIMIT 1", (uid,))
            user = await c.fetchone()
            if not user:
                return None
            first_name = user.get("first_name") or ""
            phone = user.get("phone") or ""
            code_value = hashlib.md5((first_name + ' ' + phone).encode('utf-8')).hexdigest()
            owner_name = (first_name + ' ' + phone).strip()
            await c.execute(
                """
                INSERT INTO referral_codes (code, owner_name, owner_user_id, code_type)
                VALUES (%s, %s, %s, 'user')
                ON DUPLICATE KEY UPDATE owner_name=VALUES(owner_name)
                """,
                (code_value, owner_name, uid),
            )
            await _maybe_await(conn.commit())
            return code_value


async def get_user_ref_code(
    tg_user_id: str = "",
    *,
    internal_id: int = 0,
    max_user_id: int | str = "",
) -> Optional[str]:
    """Получить реферальный код. Можно передать tg_user_id, max_user_id или internal_id."""
    uid = await _resolve_public_user_id(
        tg_user_id or None,
        internal_user_id=internal_id or None,
        max_user_id=max_user_id or None,
    )
    if not uid:
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT code FROM referral_codes WHERE owner_user_id=%s LIMIT 1", (uid,))
            r = await c.fetchone()
            return (r.get("code") if r else None) or None

async def get_user_phone(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> Optional[str]:
    row = await _get_user_row_by_any_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    return (row.get("phone") if row else None) or None

async def get_user_kkt_factory_number(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> Optional[str]:
    row = await _get_user_row_by_any_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    return (row.get("kkt_factory_number") if row else None) or None


async def get_user_we_added(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> bool:
    row = await _get_user_row_by_any_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    return bool(row and row.get("we_added"))



# ===== Helpers: internal users.id based getters (for orders/admin flows) =====
async def get_user_inn_by_id(internal_user_id: int) -> Optional[str]:
    try:
        uid = int(internal_user_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT inn FROM users WHERE id=%s LIMIT 1", (uid,))
            r = await c.fetchone()
            return (r.get("inn") if r else None) or None


async def get_user_phone_by_id(internal_user_id: int) -> Optional[str]:
    try:
        uid = int(internal_user_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT phone FROM users WHERE id=%s LIMIT 1", (uid,))
            r = await c.fetchone()
            return (r.get("phone") if r else None) or None


async def get_user_kkt_factory_number_by_id(internal_user_id: int) -> Optional[str]:
    try:
        uid = int(internal_user_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT kkt_factory_number FROM users WHERE id=%s LIMIT 1", (uid,))
            r = await c.fetchone()
            return (r.get("kkt_factory_number") if r else None) or None



async def set_user_inn(
    user_id: str | int = "",
    inn: Optional[str] = None,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    inn_n = _norm_inn(inn) if inn else None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "UPDATE users SET inn=%s, updated_at=NOW() WHERE id=%s",
                (inn_n, int(uid)),
            )
            await _sync_user_verified_flag_by_id(c, int(uid))
            await _maybe_await(conn.commit())

    # Loyalty: бонус за заполнение ИНН
    if inn_n:
        await _try_award_profile_field(int(uid), "inn")


async def set_user_kkt_factory_number(
    user_id: str | int = "",
    kkt_number: Optional[str] = None,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "UPDATE users SET kkt_factory_number=%s, updated_at=NOW() WHERE id=%s",
                (kkt_number, int(uid)),
            )
            await _maybe_await(conn.commit())


async def set_user_address(
    user_id: str | int = "",
    address: Optional[str] = None,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "UPDATE users SET address=%s, updated_at=NOW() WHERE id=%s",
                (address, int(uid)),
            )
            await _maybe_await(conn.commit())


async def set_user_city(
    user_id: str | int = "",
    city: Optional[str] = None,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "UPDATE users SET city=%s, updated_at=NOW() WHERE id=%s",
                (city, int(uid)),
            )
            await _maybe_await(conn.commit())

    # Loyalty: бонус за заполнение города
    if city:
        await _try_award_profile_field(int(uid), "city")


async def set_user_phone(
    user_id: str | int = "",
    phone: Optional[str] = None,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    phone_n = _norm_phone(phone) if phone else None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "UPDATE users SET phone=%s, updated_at=NOW() WHERE id=%s",
                (phone_n, int(uid)),
            )
            await _sync_user_verified_flag_by_id(c, int(uid))
            await _maybe_await(conn.commit())


async def get_all_user_ids() -> List[str]:
    """
    Нужно для рассылок/напоминаний.
    Возвращает список tg_user_id как строки.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            ids: set[str] = set()
            await c.execute("SELECT tg_user_id FROM users WHERE tg_user_id IS NOT NULL")
            for r in await c.fetchall():
                ids.add(str(r["tg_user_id"]))
            # Также собираем из orders через join с users
            await c.execute("""
                SELECT DISTINCT u.tg_user_id
                FROM orders o
                JOIN users u ON u.id = o.user_id
                WHERE u.tg_user_id IS NOT NULL
            """)
            for r in await c.fetchall():
                ids.add(str(r["tg_user_id"]))
            return list(ids)


# ---- consent / verified ----
async def set_user_consent(
    user_id: str | int = "",
    version: str = "1",
    method: str = "telegram",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    """
    Устанавливает согласие пользователя.
    В новой схеме consent_version и consent_method убраны,
    используем consent_accepted (bool) и consent_accepted_at.
    """
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE users
                   SET consent_accepted = 1,
                       consent_accepted_at = COALESCE(consent_accepted_at, NOW()),
                       updated_at = NOW()
                 WHERE id=%s
                """,
                (int(uid),),
            )
            await _maybe_await(conn.commit())


async def get_user_consent(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает информацию о согласии.
    Для обратной совместимости добавляем consent_version = "1" если consent_accepted = true.
    """
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT consent_accepted, consent_accepted_at
                  FROM users
                 WHERE id=%s
                 LIMIT 1
                """,
                (int(uid),),
            )
            row = await c.fetchone()
            if row:
                # Добавляем поля для обратной совместимости
                row["consent_version"] = "1" if row.get("consent_accepted") else None
                row["consent_method"] = (
                    "max" if max_user_id is not None and row.get("consent_accepted")
                    else "telegram" if row.get("consent_accepted") else None
                )
            return row


async def mark_user_verified(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    """
    Помечает пользователя как верифицированного.
    В новой схеме используем is_verified (bool) вместо verified_at.
    """
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _sync_user_verified_flag_by_id(c, int(uid))
            await _maybe_await(conn.commit())

    # Loyalty: наградить реферера за верификацию приглашённого
    try:
        from services.referral_engine import award_referral_verification
        await award_referral_verification(int(uid))
    except Exception:
        _log.debug("Referral verification reward failed for user=%s", uid, exc_info=True)


async def is_user_verified(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> bool:
    """
    Проверяет, верифицирован ли пользователь.
    В новой схеме проверяем is_verified (bool).
    """
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return False
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT is_verified, tg_user_id FROM users WHERE id=%s LIMIT 1", (int(uid),))
            r = await c.fetchone()
            is_user_ver = bool(r and r.get("is_verified"))
            if not is_user_ver:
                from db.chats import get_specialist_by_tg
                tg_uid = r.get("tg_user_id") if r else None
                spec = await get_specialist_by_tg(tg_user_id=tg_uid, tg_username=None) if tg_uid else None
                if not spec:
                    return False
            return True


async def get_nav_snapshot(
    user_id: str | int,
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
    tg_username: str | None = None,
    consent_version_required: int = 1,
) -> Dict[str, Any]:
    """
    Быстрый снимок данных для построения главного меню (nav_kb) за ОДНО соединение с БД.
    """
    del consent_version_required
    tg_username_n = (tg_username or "").strip().lstrip("@") or None
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return {
            "user": {},
            "spec": None,
            "is_spec": False,
            "consent_ok": False,
            "verified": False,
            "phone_confirmed": False,
            "inn_ok": False,
            "tg_username": tg_username_n,
        }

    async with get_conn() as conn:
        cur = await _cur(conn)
        try:
            # 1) users - используем внутренний users.id
            await _maybe_await(
                cur.execute(
                    """
                    SELECT
                      id, tg_user_id, max_user_id, phone, inn, tg_username,
                      consent_accepted, consent_accepted_at,
                      is_verified
                    FROM users
                    WHERE id=%s
                    LIMIT 1
                    """,
                    (int(uid),),
                )
            )
            user = await _maybe_await(cur.fetchone()) or {}
            # Добавляем user_id для обратной совместимости
            if user:
                user["user_id"] = str(user.get("id") or "")

            if not tg_username_n:
                tg_username_n = (user.get("tg_username") or "").strip().lstrip("@") or None

            # 2) specialists (внутри того же conn)
            spec: Dict[str, Any] | None = None
            tg_user_id = user.get("tg_user_id")
            max_user_id_db = user.get("max_user_id")

            # 2.1 приоритетно по tg_user_id
            if tg_user_id:
                await _maybe_await(
                    cur.execute(
                        "SELECT * FROM specialists WHERE tg_user_id=%s AND is_active=1 LIMIT 1",
                        (int(tg_user_id),),
                    )
                )
                spec = await _maybe_await(cur.fetchone())

                # обновим username на всякий
                if spec and tg_username_n and (spec.get("tg_username") != tg_username_n):
                    await _maybe_await(
                        cur.execute(
                            "UPDATE specialists SET tg_username=%s WHERE id=%s",
                            (tg_username_n, int(spec["id"])),
                        )
                    )
                    await _maybe_await(conn.commit())
                    spec["tg_username"] = tg_username_n

            # 2.2 иначе по username
            if not spec and tg_username_n:
                await _maybe_await(
                    cur.execute(
                        "SELECT * FROM specialists WHERE tg_username=%s AND is_active=1 LIMIT 1",
                        (tg_username_n,),
                    )
                )
                spec = await _maybe_await(cur.fetchone())

                # если нашли по username и знаем tg_user_id — привяжем автоматически
                if spec and tg_user_id:
                    try:
                        cur_tid = spec.get("tg_user_id")
                        cur_tid_i = int(cur_tid) if cur_tid is not None else None
                    except Exception:
                        cur_tid_i = None

                    if cur_tid_i != int(tg_user_id):
                        await _maybe_await(
                            cur.execute(
                                "UPDATE specialists SET tg_user_id=%s, tg_username=%s WHERE id=%s",
                                (int(tg_user_id), tg_username_n, int(spec["id"])),
                            )
                        )
                        await _maybe_await(conn.commit())
                        spec["tg_user_id"] = int(tg_user_id)
                        spec["tg_username"] = tg_username_n

            if not spec and max_user_id_db:
                await _maybe_await(
                    cur.execute(
                        "SELECT * FROM specialists WHERE max_user_id=%s AND is_active=1 LIMIT 1",
                        (int(max_user_id_db),),
                    )
                )
                spec = await _maybe_await(cur.fetchone())

            # 3) вычисляем флаги
            phone_confirmed = bool(user.get("phone"))

            inn_digits = re.sub(r"\D+", "", str(user.get("inn") or ""))
            inn_ok = len(inn_digits) in (10, 12)

            # В новой схеме consent_accepted - это bool
            consent_ok = bool(user.get("consent_accepted")) or bool(user.get("consent_accepted_at"))

            # Партнёры (profession=5) в ботах работают как клиенты, не как специалисты
            is_partner_spec = bool(spec) and int((spec or {}).get("profession") or 0) == 5

            # логика как в is_user_verified(): либо is_verified, либо это специалист (включая партнёра)
            verified = bool(user.get("is_verified")) or bool(spec)

            return {
                "user": user,
                "spec": spec,
                "is_spec": bool(spec) and not is_partner_spec,
                "consent_ok": consent_ok,
                "verified": verified,
                "phone_confirmed": phone_confirmed,
                "inn_ok": inn_ok,
                "tg_username": tg_username_n,
            }
        finally:
            try:
                await _maybe_await(cur.close())
            except Exception:
                pass



async def reset_user_profile(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> None:
    """
    Мягкий сброс (как /reset_me): оставляем tg_username/consent,
    но чистим профильные поля.
    """
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE users
                   SET first_name=NULL,
                       last_name=NULL,
                       phone=NULL,
                       email=NULL,
                       inn=NULL,
                       org_name=NULL,
                       kpp=NULL,
                       city=NULL,
                       is_verified=0,
                       updated_at=NOW()
                 WHERE id=%s
                """,
                (int(uid),),
            )
            await _maybe_await(conn.commit())



async def wipe_user_telegram_identity(tg_user_id: str | int = "", *, internal_user_id: int | str | None = None) -> Dict[str, int]:
    """Soft wipe: keep client record but remove Telegram identity and verification flags."""
    uid = await resolve_internal_user_id(
        internal_user_id=internal_user_id,
        tg_user_id=tg_user_id or None,
    )
    if not uid:
        return {"users_updated": 0}
    updated = 0
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE users
                SET
                    tg_user_id = NULL,
                    tg_username = NULL,
                    first_name = NULL,
                    last_name = NULL,
                    is_verified = 0,
                    consent_accepted = 0,
                    consent_accepted_at = NULL
                WHERE id = %s
                """,
                (int(uid),),
            )
            updated = c.rowcount or 0
        await conn.commit()
    return {"users_updated": int(updated)}


async def wipe_user_max_identity(max_user_id: str | int) -> Dict[str, int]:
    """Soft wipe for MAX: keep row, remove MAX identity and registration profile fields."""
    try:
        max_uid = int(max_user_id)
    except (ValueError, TypeError):
        return {"users_updated": 0}

    updated = 0
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE users
                SET
                    max_user_id = NULL,
                    first_name = NULL,
                    last_name = NULL,
                    phone = NULL,
                    email = NULL,
                    inn = NULL,
                    org_name = NULL,
                    kpp = NULL,
                    city = NULL,
                    is_verified = 0,
                    consent_accepted = 0,
                    consent_accepted_at = NULL,
                    updated_at = NOW()
                WHERE max_user_id = %s
                """,
                (max_uid,),
            )
            updated = c.rowcount or 0
        await _maybe_await(conn.commit())
    return {"users_updated": int(updated)}


async def reset_user_profile_by_internal_id(internal_id: int) -> None:
    """
    Мягкий сброс профиля по internal users.id (для MAX-бота).
    Чистим профильные поля, сбрасываем верификацию — но сохраняем
    запись пользователя и max_user_id.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE users
                   SET first_name=NULL,
                       last_name=NULL,
                       phone=NULL,
                       email=NULL,
                       inn=NULL,
                       org_name=NULL,
                       kpp=NULL,
                       city=NULL,
                       is_verified=0,
                       consent_accepted=0,
                       consent_accepted_at=NULL,
                       updated_at=NOW()
                 WHERE id=%s
                """,
                (internal_id,),
            )
            await _maybe_await(conn.commit())


# =========================================================
# MAX Messenger User Support
# =========================================================

async def _get_user_id_by_max(max_user_id: int | str) -> int | None:
    """Получить внутренний id пользователя по max_user_id."""
    try:
        max_uid = int(max_user_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT id FROM users WHERE max_user_id=%s LIMIT 1", (max_uid,))
            row = await c.fetchone()
            return int(row["id"]) if row else None


async def _get_or_create_max_user_id(max_user_id: int | str) -> int:
    """Получить или создать внутренний id пользователя по max_user_id."""
    try:
        max_uid = int(max_user_id)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid max_user_id: {max_user_id}")

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO users (max_user_id, we_added, created_at, updated_at)
                VALUES (%s, 0, NOW(), NOW())
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    updated_at = NOW()
                """,
                (max_uid,),
            )
            await _maybe_await(conn.commit())
            rid = int(getattr(c, "lastrowid", 0) or 0)
            if rid:
                return rid

    existing = await _get_user_id_by_max(max_uid)
    return int(existing or 0)


async def upsert_max_user(
    max_user_id: str,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> int:
    """
    Создаёт или обновляет пользователя по MAX user ID.
    Возвращает внутренний users.id.
    """
    try:
        max_uid = int(max_user_id)
    except (ValueError, TypeError):
        return 0

    phone_n = _norm_phone(phone) if phone else None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            linked_id = await find_existing_user_for_link(phone=phone_n)
            if linked_id:
                await c.execute("SELECT max_user_id FROM users WHERE id=%s LIMIT 1", (int(linked_id),))
                existing = await c.fetchone() or {}
                if not existing.get("max_user_id"):
                    await c.execute(
                        """
                        UPDATE users
                        SET max_user_id=%s,
                            first_name = COALESCE(%s, first_name),
                            last_name = COALESCE(%s, last_name),
                            phone = COALESCE(%s, phone),
                            email = COALESCE(%s, email),
                            updated_at = NOW()
                        WHERE id=%s
                        """,
                        (max_uid, first_name, last_name, phone_n, email, int(linked_id)),
                    )
                    if phone_n is not None:
                        await _sync_user_verified_flag_by_id(c, int(linked_id))
                    await _maybe_await(conn.commit())
                    return int(linked_id)
            await c.execute(
                """
                INSERT INTO users (max_user_id, first_name, last_name, phone, email, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                ON DUPLICATE KEY UPDATE
                  id = LAST_INSERT_ID(id),
                  first_name  = COALESCE(VALUES(first_name),  first_name),
                  last_name   = COALESCE(VALUES(last_name),   last_name),
                  phone       = COALESCE(VALUES(phone),       phone),
                  email       = COALESCE(VALUES(email),       email),
                  updated_at  = NOW()
                """,
                (max_uid, first_name, last_name, phone_n, email),
            )

            internal_id = int(getattr(c, "lastrowid", 0) or 0)

            if phone_n is not None and internal_id:
                await _sync_user_verified_flag_by_id(c, internal_id)

            await _maybe_await(conn.commit())

            if internal_id:
                return internal_id

    existing = await _get_user_id_by_max(max_uid)
    return int(existing or 0)


async def get_user_by_max(max_user_id: str) -> Optional[Dict[str, Any]]:
    """Получить пользователя по MAX user ID."""
    return await _get_user_row_by_any_id(max_user_id=max_user_id)


async def get_user_by_internal_id(internal_id: int) -> Optional[Dict[str, Any]]:
    """Получить пользователя по внутреннему users.id."""
    return await _get_user_row_by_any_id(internal_user_id=internal_id)


async def _sync_user_verified_flag_by_id(c, internal_id: int) -> bool:
    """Синхронизирует users.is_verified по внутреннему id."""
    await c.execute(
        "SELECT inn, phone FROM users WHERE id=%s LIMIT 1",
        (int(internal_id),),
    )
    row = await c.fetchone() or {}
    verified = bool(_norm_inn(row.get("inn")) and _norm_phone(row.get("phone")))
    await c.execute(
        "UPDATE users SET is_verified=%s, updated_at=NOW() WHERE id=%s",
        (int(verified), int(internal_id)),
    )
    return verified


async def update_user_by_internal_id(internal_id: int, **fields) -> None:
    """
    Обновляет поля пользователя по внутреннему users.id.
    Поддерживает: first_name, last_name, phone, email, city, inn.
    Автоматически синхронизирует is_verified при изменении phone/inn.
    """
    ALLOWED = {"first_name", "last_name", "phone", "email", "city", "inn", "address"}
    to_set = {k: v for k, v in fields.items() if k in ALLOWED and v is not None}
    if not to_set:
        return

    if "phone" in to_set:
        to_set["phone"] = _norm_phone(to_set["phone"])
    if "inn" in to_set:
        to_set["inn"] = _norm_inn(to_set["inn"])

    set_parts = [f"{k}=%s" for k in to_set]
    set_parts.append("updated_at=NOW()")
    values = list(to_set.values()) + [int(internal_id)]

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                f"UPDATE users SET {', '.join(set_parts)} WHERE id=%s",
                tuple(values),
            )
            if "phone" in to_set or "inn" in to_set:
                await _sync_user_verified_flag_by_id(c, int(internal_id))
            await _maybe_await(conn.commit())

    # Loyalty: бонус за заполнение полей профиля
    for field in ("phone", "email", "inn", "city"):
        if field in to_set and to_set[field]:
            await _try_award_profile_field(int(internal_id), field)


async def set_user_consent_by_internal_id(internal_id: int, version: str = "1", method: str = "max") -> None:
    """Устанавливает согласие пользователя по внутреннему id."""
    try:
        uid = int(internal_id)
    except (ValueError, TypeError):
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                UPDATE users
                   SET consent_accepted = 1,
                       consent_accepted_at = COALESCE(consent_accepted_at, NOW()),
                       updated_at = NOW()
                 WHERE id=%s
                """,
                (uid,),
            )
            await _maybe_await(conn.commit())


async def get_user_consent_by_internal_id(internal_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает информацию о согласии по внутреннему id."""
    try:
        uid = int(internal_id)
    except (ValueError, TypeError):
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT consent_accepted, consent_accepted_at FROM users WHERE id=%s LIMIT 1",
                (uid,),
            )
            row = await c.fetchone()
            if row:
                row["consent_version"] = "1" if row.get("consent_accepted") else None
                row["consent_method"] = "max" if row.get("consent_accepted") else None
            return row


async def mark_user_verified_by_internal_id(internal_id: int) -> None:
    """Помечает пользователя как верифицированного по внутреннему id."""
    try:
        uid = int(internal_id)
    except (ValueError, TypeError):
        return
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _sync_user_verified_flag_by_id(c, uid)
            await _maybe_await(conn.commit())

    # Loyalty: наградить реферера за верификацию приглашённого
    try:
        from services.referral_engine import award_referral_verification
        await award_referral_verification(uid)
    except Exception:
        _log.debug("Referral verification reward failed for user=%s", uid, exc_info=True)


async def is_user_verified_by_internal_id(internal_id: int) -> bool:
    """Проверяет, верифицирован ли пользователь по внутреннему id."""
    try:
        uid = int(internal_id)
    except (ValueError, TypeError):
        return False
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT is_verified FROM users WHERE id=%s LIMIT 1", (uid,))
            r = await c.fetchone()
            is_ver = bool(r and r.get("is_verified"))
            if not is_ver:
                from db.chats import get_specialist_by_tg
                # Проверяем, может ли быть специалистом (по tg или max)
                await c.execute(
                    "SELECT tg_user_id FROM users WHERE id=%s LIMIT 1", (uid,)
                )
                urow = await c.fetchone()
                tg_uid = urow.get("tg_user_id") if urow else None
                if tg_uid:
                    spec = await get_specialist_by_tg(tg_user_id=str(tg_uid), tg_username=None)
                    if spec:
                        return True
            return is_ver


async def wipe_user_all(
    user_id: str | int = "",
    *,
    internal_user_id: int | str | None = None,
    max_user_id: int | str | None = None,
) -> Dict[str, int]:
    """
    Жёсткий сброс (как /wipe_me) — ДЛЯ ТЕСТОВ.
    ВАЖНО: мы возвращаем коды в пул (делает систему «как с нуля»),
    поэтому на проде лучше ограничить админом.
    """
    uid = await _resolve_public_user_id(
        user_id,
        internal_user_id=internal_user_id,
        max_user_id=max_user_id,
    )
    if not uid:
        return {"orders_deleted": 0, "codes_reset": 0, "users_deleted": 0}

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            # Получим внутренний id пользователя
            await c.execute("SELECT id FROM users WHERE id=%s LIMIT 1", (int(uid),))
            user_row = await c.fetchone()
            if not user_row:
                await _maybe_await(conn.commit())
                return {"orders_deleted": 0, "codes_reset": 0, "users_deleted": 0}

            internal_user_id = int(user_row["id"])

            # Найдём коды, выданные этому пользователю (чтобы вернуть в пул)
            await c.execute("SELECT id FROM codes WHERE user_id=%s AND is_used=1", (internal_user_id,))
            code_ids = [r["id"] for r in await c.fetchall()]

            # Удалим заказы пользователя (order_items каскадом)
            await c.execute("DELETE FROM orders WHERE user_id=%s", (internal_user_id,))
            orders_deleted = c.rowcount

            # Вернём коды (если были)
            codes_reset = 0
            if code_ids:
                fmt = ",".join(["%s"] * len(code_ids))
                await c.execute(
                    f"""
                    UPDATE codes
                       SET is_used=0, user_id=NULL, issued_at=NULL
                     WHERE id IN ({fmt})
                    """,
                    tuple(code_ids),
                )
                codes_reset = c.rowcount

            # Удалим пользователя
            await c.execute("DELETE FROM users WHERE id=%s", (internal_user_id,))
            users_deleted = c.rowcount

            await _maybe_await(conn.commit())
            return {"orders_deleted": orders_deleted, "codes_reset": codes_reset, "users_deleted": users_deleted}
