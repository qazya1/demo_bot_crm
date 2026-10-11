from __future__ import annotations
import hashlib
import logging
from typing import Any, Dict, Optional
from db.connection import (
    get_conn, _cur, _maybe_await, _begin_tx,
    _norm_phone,
    IntegrityError,
)

logger = logging.getLogger(__name__)


# =========================================================
# Referral (секретные промокоды / источники клиентов)
# =========================================================


async def ensure_referral_schema(add_users_fk: bool = True) -> None:
    """
    Создаёт таблицы referral_codes / referral_activations.
    Параметр add_users_fk сохранён для обратной совместимости и игнорируется.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:

            await c.execute("""
                CREATE TABLE IF NOT EXISTS referral_codes (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  code VARCHAR(32) NOT NULL,
                  owner_name VARCHAR(128) NOT NULL,
                  owner_note VARCHAR(255) NULL,
                  is_active TINYINT NOT NULL DEFAULT 1,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE KEY uq_ref_code (code),
                  KEY ix_ref_active (is_active)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            await c.execute("""
                CREATE TABLE IF NOT EXISTS referral_activations (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  user_id VARCHAR(64) NOT NULL,
                  referral_code_id BIGINT NOT NULL,
                  activated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  raw_text VARCHAR(64) NULL,
                  source_platform VARCHAR(16) NULL,
                  UNIQUE KEY uq_user_once (user_id),
                  KEY ix_ref_code (referral_code_id),
                  CONSTRAINT fk_ra_code
                    FOREIGN KEY (referral_code_id) REFERENCES referral_codes(id)
                    ON DELETE RESTRICT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # Миграция: добавляем source_platform если ещё нет
            try:
                await c.execute("""
                    ALTER TABLE referral_activations
                    ADD COLUMN source_platform VARCHAR(16) NULL
                """)
            except Exception:
                pass  # колонка уже существует

            for sql in (
                "ALTER TABLE referral_codes ADD COLUMN owner_user_id BIGINT NULL",
                "ALTER TABLE referral_codes ADD COLUMN code_type VARCHAR(16) NULL",
            ):
                try:
                    await c.execute(sql)
                except Exception:
                    pass

            try:
                await c.execute(
                    """
                    UPDATE referral_codes
                    SET code_type = CASE
                        WHEN owner_user_id IS NOT NULL THEN 'user'
                        WHEN owner_note LIKE 'specialist:%' THEN 'user'
                        ELSE 'manual'
                    END
                    WHERE code_type IS NULL OR code_type = ''
                    """
                )
            except Exception:
                pass

            await _maybe_await(conn.commit())


async def upsert_referral_code(
    code: str,
    owner_name: str,
    owner_note: str | None = None,
    is_active: bool = True,
    *,
    code_type: str | None = "manual",
    owner_user_id: int | None = None,
) -> None:
    code = (code or "").strip()
    if not code:
        return
    await ensure_referral_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                INSERT INTO referral_codes (code, owner_name, owner_note, is_active, code_type, owner_user_id)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  owner_name = VALUES(owner_name),
                  owner_note = VALUES(owner_note),
                  is_active  = VALUES(is_active),
                  code_type = COALESCE(VALUES(code_type), code_type),
                  owner_user_id = COALESCE(VALUES(owner_user_id), owner_user_id)
                """,
                (code, owner_name, owner_note, 1 if is_active else 0, (code_type or "manual")[:16], owner_user_id),
            )
            await _maybe_await(conn.commit())


async def get_or_create_specialist_ref_code(specialist_id: int, owner_name: str | None = None) -> Optional[str]:
    """
    Получить (или создать) персональный referral code для специалиста.
    Код привязывается по owner_note='specialist:<id>' и используется для deep-link ref_<code>.
    """
    sid = int(specialist_id or 0)
    if sid <= 0:
        return None
    await ensure_referral_schema()

    owner_note = f"specialist:{sid}"
    owner = (owner_name or f"Специалист #{sid}").strip() or f"Специалист #{sid}"
    digest = hashlib.md5(f"{sid}:{owner}".encode("utf-8")).hexdigest()
    prefix = f"sp{sid}_"
    suffix_len = max(0, 32 - len(prefix))
    code = (prefix + digest[:suffix_len]) if suffix_len > 0 else digest[:32]

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT code FROM referral_codes WHERE owner_note=%s LIMIT 1",
                (owner_note,),
            )
            row = await c.fetchone()
            if row and row.get("code"):
                return str(row["code"])

            await c.execute(
                """
                INSERT INTO referral_codes (code, owner_name, owner_note, is_active, code_type)
                VALUES (%s, %s, %s, 1, 'user')
                ON DUPLICATE KEY UPDATE
                  owner_name = VALUES(owner_name),
                  owner_note = VALUES(owner_note),
                  is_active  = 1,
                  code_type = 'user'
                """,
                (code, owner, owner_note),
            )
            await _maybe_await(conn.commit())

            await c.execute("SELECT code FROM referral_codes WHERE owner_note=%s LIMIT 1", (owner_note,))
            row = await c.fetchone()
            return (row.get("code") if row else None) or None


async def get_referral_code(code: str) -> Optional[Dict[str, Any]]:
    code = (code or "").strip()
    if not code:
        return None
    await ensure_referral_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT * FROM referral_codes WHERE code=%s AND is_active=1 LIMIT 1",
                (code,),
            )
            return await c.fetchone()


async def get_user_referral(user_id: str) -> Optional[Dict[str, Any]]:
    user_id = str(user_id)
    await ensure_referral_schema()
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT rc.*
                  FROM referral_activations ra
                  JOIN referral_codes rc ON rc.id = ra.referral_code_id
                 WHERE ra.user_id=%s
                 ORDER BY ra.id DESC
                 LIMIT 1
                """,
                (user_id,),
            )
            return await c.fetchone()


async def apply_referral_code(
    tg_user_id: int = 0,
    raw_code: str = "",
    raw_text: str | None = None,
    *,
    internal_id: int = 0,
    max_user_id: int = 0,
    source_platform: str | None = None,
) -> Dict[str, Any]:
    """
    Привязывает пользователя к referral-коду (один раз).

    Можно передать tg_user_id (для TG-бота) или internal_id (для MAX-бота).
    source_platform: 'telegram' или 'max' — откуда пришёл пользователь.
    Возвращает:
      {"ok": True, "status": "applied", "code": "...", "owner_name": "..."}
      {"ok": True, "status": "already_set", ...}
      {"ok": False, "status": "not_found"}
    """
    from db.users import resolve_internal_user_id

    def _normalize_platform(value: str | None) -> str | None:
        normalized = (value or "").strip().lower()
        return normalized if normalized in ("telegram", "max") else None

    def _normalize_referral_code(value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        lowered = raw.lower()
        if lowered.startswith("start=ref_"):
            return raw[10:].strip()
        if lowered.startswith("ref_"):
            return raw[4:].strip()
        if "ref_" in lowered:
            idx = lowered.find("ref_")
            candidate = raw[idx + 4:]
            for delimiter in ("&", "?", " ", "\n", "\t"):
                if delimiter in candidate:
                    candidate = candidate.split(delimiter, 1)[0]
            return candidate.strip()
        return raw

    code = _normalize_referral_code(raw_code)
    logger.info(
        "[REF] apply_referral_code start: tg_user_id=%s internal_id=%s max_user_id=%s source=%s raw_code=%r normalized_code=%r raw_text=%r",
        tg_user_id,
        internal_id,
        max_user_id,
        source_platform,
        raw_code,
        code,
        raw_text,
    )

    if not code:
        return {"ok": False, "status": "not_found"}
    await ensure_referral_schema()

    internal_user_id = await resolve_internal_user_id(
        internal_user_id=internal_id or None,
        tg_user_id=tg_user_id or None,
        max_user_id=max_user_id or None,
        create=True,
    )
    if not internal_user_id:
        logger.warning("[REF] resolve_internal_user_id failed: tg_user_id=%s internal_id=%s max_user_id=%s", tg_user_id, internal_id, max_user_id)
        return {"ok": False, "status": "not_found"}

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            # 1) найдём активный код
            await c.execute("SELECT * FROM referral_codes WHERE code=%s AND is_active=1 LIMIT 1", (code,))
            rc = await c.fetchone()
            if not rc:
                logger.info("[REF] referral code not found or inactive: normalized_code=%r internal_user_id=%s", code, internal_user_id)
                await _maybe_await(conn.commit())
                return {"ok": False, "status": "not_found"}

            rc_id = int(rc["id"])

            owner_user_id = int(rc.get("owner_user_id") or 0)
            owner_note = str(rc.get("owner_note") or "")
            platform = _normalize_platform(source_platform)
            owner_partner_id = 0
            if owner_note.startswith("specialist:"):
                try:
                    specialist_id = int(owner_note.split(":", 1)[1])
                except Exception:
                    specialist_id = 0
                if specialist_id > 0:
                    await c.execute(
                        "SELECT id FROM specialists WHERE id=%s AND profession=5 LIMIT 1",
                        (specialist_id,),
                    )
                    spec_row = await c.fetchone()
                    owner_partner_id = int((spec_row or {}).get("id") or 0)

            # 2) в историю (one per user)
            await c.execute(
                "SELECT id, source_platform FROM referral_activations WHERE user_id=%s LIMIT 1",
                (internal_user_id,),
            )
            existing = await c.fetchone()
            already_set = bool(existing)
            if not already_set:
                await c.execute(
                    """
                    INSERT INTO referral_activations (user_id, referral_code_id, raw_text, source_platform)
                    VALUES (%s,%s,%s,%s)
                    """,
                    (internal_user_id, rc_id, (raw_text or "")[:64] if raw_text else None,
                     platform),
                )
                logger.info(
                    "[REF] referral_activations INSERT: user_id=%s referral_code_id=%s source_platform=%s raw_text=%r",
                    internal_user_id, rc_id, platform, (raw_text or "")[:64] if raw_text else None,
                )
            elif platform and not (existing or {}).get("source_platform"):
                await c.execute(
                    "UPDATE referral_activations SET source_platform=%s WHERE id=%s",
                    (platform, existing["id"]),
                )
                logger.info(
                    "[REF] referral_activations UPDATE source_platform: activation_id=%s user_id=%s source_platform=%s",
                    existing["id"], internal_user_id, platform,
                )
            else:
                logger.info(
                    "[REF] referral_activations already_set: activation_id=%s user_id=%s existing_source_platform=%s",
                    (existing or {}).get("id"), internal_user_id, (existing or {}).get("source_platform"),
                )

            if owner_partner_id:
                await c.execute(
                    """
                    UPDATE users
                    SET
                      source_partner_id = CASE
                        WHEN source_partner_id IS NULL OR source_partner_id = %s THEN %s
                        ELSE source_partner_id
                      END,
                      partner_source_type = CASE
                        WHEN source_partner_id IS NULL OR source_partner_id = %s THEN 'referral'
                        ELSE partner_source_type
                      END,
                      updated_at = NOW()
                    WHERE id = %s
                    """,
                    (owner_partner_id, owner_partner_id, owner_partner_id, internal_user_id),
                )

            await _maybe_await(conn.commit())
            logger.info(
                "[REF] apply_referral_code committed: status=%s user_id=%s referral_code_id=%s owner_user_id=%s",
                "already_set" if already_set else "applied",
                internal_user_id,
                rc_id,
                owner_user_id,
            )

    # Loyalty / QR-collection hooks
    try:
        from services.referral_engine import (
            award_referral_activation,
            process_qr_collection_reward,
            register_qr_collection_exchange,
        )

        if owner_user_id:
            if already_set:
                # Спец-кейс QR-collection: реферер сканирует QR приглашённого.
                # Это не меняет привязку реферала, но может засчитать «обмен QR».
                await register_qr_collection_exchange(internal_user_id, owner_user_id)
                await process_qr_collection_reward(internal_user_id)
            else:
                await award_referral_activation(owner_user_id, internal_user_id)
                await process_qr_collection_reward(owner_user_id)
    except Exception:
        import logging
        logging.getLogger(__name__).debug(
            "Referral loyalty failed for user=%s code=%s", internal_user_id, code, exc_info=True,
        )

    return {
        "ok": True,
        "status": "already_set" if already_set else "applied",
        "code": rc.get("code"),
        "owner_name": rc.get("owner_name"),
    }
