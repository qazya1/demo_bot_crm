from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple
from db.connection import (
    get_conn, _cur, _maybe_await, _begin_tx,
    _norm_inn, _norm_phone, _phone_last10,
)


# =========================================================
# Clients + верификация
# В новой схеме таблица clients объединена с users.
# Функции ниже работают с таблицей users.
# =========================================================
async def get_client_by_inn(inn: str) -> Optional[Dict[str, Any]]:
    """
    Поиск пользователя по ИНН.
    В новой схеме clients объединены с users.

    Важно: после миграции ИНН может встречаться в нескольких строках users.
    Выбираем наиболее "полную" карточку (приоритет: наличие client_kkt, затем tg_user_id).
    """
    inn_n = _norm_inn(inn)
    if not inn_n:
        return None
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT u.*,
                       EXISTS(SELECT 1 FROM client_kkt ck WHERE ck.client_id=u.id LIMIT 1) AS has_kkt
                  FROM users u
                 WHERE u.inn=%s
                 ORDER BY has_kkt DESC,
                          (u.tg_user_id IS NOT NULL) DESC,
                          u.id DESC
                 LIMIT 1
                """,
                (inn_n,),
            )
            row = await c.fetchone()
            if row:
                row["client_id"] = row.get("id")
            return row



async def get_client_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    """
    Поиск пользователя по телефону: сравниваем последние 10 цифр.
    В новой схеме clients объединены с users.

    Важно: телефон может встречаться в нескольких строках users.
    Выбираем наиболее "полную" карточку (приоритет: наличие client_kkt, затем наличие ИНН, затем tg_user_id).
    """
    last10 = _phone_last10(phone)
    if not last10:
        return None

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT u.*,
                       EXISTS(SELECT 1 FROM client_kkt ck WHERE ck.client_id=u.id LIMIT 1) AS has_kkt
                  FROM users u
                 WHERE RIGHT(
                     REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(u.phone,'+',''),'-',''),' ',''),'(',''),')',''),'.',''),
                     10
                 ) = %s
                 ORDER BY has_kkt DESC,
                          (u.inn IS NOT NULL AND u.inn <> '') DESC,
                          (u.tg_user_id IS NOT NULL) DESC,
                          u.id DESC
                 LIMIT 1
                """,
                (last10,),
            )
            row = await c.fetchone()
            if row:
                row["client_id"] = row.get("id")
            return row



async def maybe_our_client(inn: str | None, phone: str | None) -> bool:
    """
    Проверяем, есть ли пользователь с таким ИНН или телефоном.
    """
    try:
        if inn and await get_client_by_inn(inn):
            return True
    except Exception:
        pass
    try:
        if phone and await get_client_by_phone(phone):
            return True
    except Exception:
        pass
    return False


async def verify_inn_phone(inn: str | None, phone: str | None) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Проверяем: пользователь с ИНН существует и телефон совпадает (после нормализации).
    Возвращаем (ok, user_or_none).
    """
    inn_n = _norm_inn(inn)
    phone_n = _norm_phone(phone)
    if not inn_n or not phone_n:
        return False, None

    client = await get_client_by_inn(inn_n)
    if not client:
        return False, None

    db_phone_n = _norm_phone(client.get("phone"))
    ok = (db_phone_n is not None and db_phone_n == phone_n)
    return (ok, client if ok else client)


async def try_merge_user_with_preloaded(
    tg_user_id: int | str,
    inn: str | None,
    phone: str | None,
) -> Dict[str, Any]:
    """
    Проверяет и (при возможности) выполняет сведение профиля Telegram-пользователя с заранее занесённым клиентом.

    Правила:
    - Верификация НЕ зависит от совпадения: бот может поставить is_verified=1 при наличии телефона+ИНН.
    - Но если в базе есть "предзагруженная" карточка (users.tg_user_id IS NULL), то при совпадении:
        * ИНН + телефон -> сводим;
        * телефон совпал, а ИНН в карточке пустой -> дописываем ИНН и сводим;
        * ИНН совпал, а телефон в карточке пустой -> дописываем телефон и сводим.
    - Если совпало только одно поле (ИНН или телефон), то возвращаем mismatch_* для показа предупреждения.
    - Если несколько кандидатов -> ambiguous.
    - Если подходящих кандидатов нет -> no_match.

    Возвращает dict:
      {status, merged, target_id, note}
    status: merged|already|no_match|mismatch_inn|mismatch_phone|ambiguous|conflict
    """
    inn_n = _norm_inn(inn)
    phone_n = _norm_phone(phone)
    last10 = _phone_last10(phone_n)
    try:
        tg_uid = int(tg_user_id)
    except Exception:
        return {"status": "no_data", "merged": False, "target_id": None, "note": None}

    if not inn_n or not phone_n or not last10:
        return {"status": "no_data", "merged": False, "target_id": None, "note": None}

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            # Текущая запись Telegram-пользователя (source)
            await c.execute(
                "SELECT * FROM users WHERE tg_user_id=%s LIMIT 1 FOR UPDATE",
                (tg_uid,),
            )
            source = await c.fetchone()
            if not source:
                await _maybe_await(conn.commit())
                return {"status": "no_source", "merged": False, "target_id": None, "note": None}

            source_id = int(source.get("id") or 0)

            # Ищем кандидатов среди предзагруженных клиентов (tg_user_id IS NULL)
            await c.execute(
                """
                SELECT *
                  FROM users
                 WHERE tg_user_id IS NULL
                   AND (
                        (inn IS NOT NULL AND inn=%s)
                        OR (
                            RIGHT(
                                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),'.',''),
                                10
                            ) = %s
                        )
                   )
                 ORDER BY id DESC
                 LIMIT 50
                """,
                (inn_n, last10),
            )
            preloaded = await c.fetchall() or []

            # Отдельно: если уже есть совпадение у ДРУГОГО tg_user_id — это конфликт
            await c.execute(
                """
                SELECT id, tg_user_id, inn, phone
                  FROM users
                 WHERE tg_user_id IS NOT NULL
                   AND tg_user_id <> %s
                   AND (
                        (inn IS NOT NULL AND inn=%s)
                        OR (
                            RIGHT(
                                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),'.',''),
                                10
                            ) = %s
                        )
                   )
                 ORDER BY id DESC
                 LIMIT 10
                """,
                (tg_uid, inn_n, last10),
            )
            conflicts = await c.fetchall() or []

            if conflicts:
                # если есть точный конфликт (оба поля совпали) — предупреждаем
                for r in conflicts:
                    if _norm_inn(r.get("inn")) == inn_n and _norm_phone(r.get("phone")) == phone_n:
                        await _maybe_await(conn.commit())
                        return {
                            "status": "conflict",
                            "merged": False,
                            "target_id": int(r.get("id") or 0),
                            "note": "Карточка с такими данными уже привязана к другому Telegram.",
                        }

            # фильтруем кандидатов: не берем source_id (на всякий)
            preloaded = [r for r in preloaded if int(r.get("id") or 0) != source_id]

            # Кандидаты для сведения
            mergeable: list[dict] = []
            mismatch_inn = False   # ИНН совпал, телефон отличается
            mismatch_phone = False # Телефон совпал, ИНН отличается

            for r in preloaded:
                rin = _norm_inn(r.get("inn"))
                rph = _norm_phone(r.get("phone"))

                match_inn = (rin == inn_n) if rin else False
                match_phone = (rph == phone_n) if rph else False

                if match_inn and match_phone:
                    mergeable.append(r)
                    continue

                # phone match, inn пустой -> дописываем ИНН и можем свести
                if match_phone and not rin:
                    mergeable.append(r)
                    continue

                # inn match, phone пустой -> дописываем телефон и можем свести
                if match_inn and not rph:
                    mergeable.append(r)
                    continue

                if match_inn and (not match_phone):
                    mismatch_inn = True
                if match_phone and (not match_inn):
                    mismatch_phone = True

            # уникализируем mergeable по id
            seen_ids = set()
            uniq_mergeable = []
            for r in mergeable:
                rid = int(r.get("id") or 0)
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    uniq_mergeable.append(r)
            mergeable = uniq_mergeable

            if len(mergeable) > 1:
                await _maybe_await(conn.commit())
                return {
                    "status": "ambiguous",
                    "merged": False,
                    "target_id": None,
                    "note": "Найдено несколько карточек клиента, требуется ручная проверка.",
                }

            if len(mergeable) == 1:
                target = mergeable[0]
                target_id = int(target.get("id") or 0)
                if not target_id or target_id == source_id:
                    await _maybe_await(conn.commit())
                    return {"status": "already", "merged": False, "target_id": target_id, "note": None}

                # Освобождаем уникальный tg_user_id (иначе не сможем присвоить его target)
                await c.execute(
                    "UPDATE users SET tg_user_id=NULL, updated_at=NOW() WHERE id=%s AND tg_user_id=%s",
                    (source_id, tg_uid),
                )

                # Переносим ссылки на пользователя
                await c.execute("UPDATE orders SET user_id=%s WHERE user_id=%s", (target_id, source_id))
                await c.execute("UPDATE codes  SET user_id=%s WHERE user_id=%s", (target_id, source_id))
                await c.execute("UPDATE client_kkt SET client_id=%s WHERE client_id=%s", (target_id, source_id))

                # Объединяем данные (заполняем пустые поля target данными source)
                await c.execute(
                    """
                    UPDATE users
                       SET tg_user_id=%s,
                           phone=COALESCE(phone, %s),
                           inn=COALESCE(inn, %s),
                           first_name=COALESCE(first_name, %s),
                           last_name=COALESCE(last_name, %s),
                           email=COALESCE(email, %s),
                           city=COALESCE(city, %s),
                           org_name=COALESCE(org_name, %s),
                           kpp=COALESCE(kpp, %s),
                           tg_username=COALESCE(tg_username, %s),
                           consent_accepted = IF(consent_accepted=1 OR %s=1, 1, 0),
                           is_verified = IF(is_verified=1 OR %s=1, 1, 0),
                           updated_at=NOW()
                     WHERE id=%s
                    """,
                    (
                        tg_uid,
                        phone_n,
                        inn_n,
                        source.get("first_name"),
                        source.get("last_name"),
                        source.get("email"),
                        source.get("city"),
                        source.get("org_name"),
                        source.get("kpp"),
                        source.get("tg_username"),
                        int(bool(source.get("consent_accepted"))),
                        int(bool(source.get("is_verified"))),
                        target_id,
                    ),
                )

                # Удаляем временную запись
                await c.execute("DELETE FROM users WHERE id=%s", (source_id,))

                await _maybe_await(conn.commit())

                # Авто-создание компании из данных контакта
                try:
                    await _auto_create_or_link_company(target_id)
                except Exception:
                    pass  # Не ломаем основной процесс

                return {
                    "status": "merged",
                    "merged": True,
                    "target_id": target_id,
                    "note": "Профиль сведён с карточкой клиента.",
                }

            # Не свели — но можем подсказать, что найдено частичное совпадение
            await _maybe_await(conn.commit())
            if mismatch_inn:
                return {
                    "status": "mismatch_inn",
                    "merged": False,
                    "target_id": None,
                    "note": "В базе найден клиент с таким ИНН, но другим телефоном.",
                }
            if mismatch_phone:
                return {
                    "status": "mismatch_phone",
                    "merged": False,
                    "target_id": None,
                    "note": "В базе найден клиент с таким телефоном, но другим ИНН.",
                }

            return {"status": "no_match", "merged": False, "target_id": None, "note": None}




async def try_merge_max_user_with_preloaded(
    internal_id: int,
    max_user_id: int,
    inn: str | None,
    phone: str | None,
) -> Dict[str, Any]:
    """
    Аналог try_merge_user_with_preloaded, но для MAX-пользователей.
    internal_id — текущий users.id пользователя MAX.
    max_user_id — MAX user ID для переноса на target.
    """
    inn_n = _norm_inn(inn)
    phone_n = _norm_phone(phone)
    last10 = _phone_last10(phone_n)

    if not internal_id:
        return {"status": "no_data", "merged": False, "target_id": None, "note": None}
    if not inn_n or not phone_n or not last10:
        return {"status": "no_data", "merged": False, "target_id": None, "note": None}

    source_id = int(internal_id)
    max_uid = int(max_user_id)

    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await _maybe_await(_begin_tx(conn))

            # Текущая запись MAX-пользователя (source)
            await c.execute(
                "SELECT * FROM users WHERE id=%s LIMIT 1 FOR UPDATE",
                (source_id,),
            )
            source = await c.fetchone()
            if not source:
                await _maybe_await(conn.commit())
                return {"status": "no_source", "merged": False, "target_id": None, "note": None}

            # Ищем кандидатов среди предзагруженных клиентов (tg_user_id IS NULL AND max_user_id IS NULL)
            await c.execute(
                """
                SELECT *
                  FROM users
                 WHERE tg_user_id IS NULL AND max_user_id IS NULL
                   AND (
                        (inn IS NOT NULL AND inn=%s)
                        OR (
                            RIGHT(
                                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),'.',''),
                                10
                            ) = %s
                        )
                   )
                 ORDER BY id DESC
                 LIMIT 50
                """,
                (inn_n, last10),
            )
            preloaded = await c.fetchall() or []

            # Конфликт: данные уже привязаны к другому пользователю
            await c.execute(
                """
                SELECT id, tg_user_id, max_user_id, inn, phone
                  FROM users
                 WHERE id <> %s
                   AND (tg_user_id IS NOT NULL OR max_user_id IS NOT NULL)
                   AND (
                        (inn IS NOT NULL AND inn=%s)
                        OR (
                            RIGHT(
                                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),'.',''),
                                10
                            ) = %s
                        )
                   )
                 ORDER BY id DESC
                 LIMIT 10
                """,
                (source_id, inn_n, last10),
            )
            conflicts = await c.fetchall() or []

            if conflicts:
                for r in conflicts:
                    if _norm_inn(r.get("inn")) == inn_n and _norm_phone(r.get("phone")) == phone_n:
                        await _maybe_await(conn.commit())
                        return {
                            "status": "conflict",
                            "merged": False,
                            "target_id": int(r.get("id") or 0),
                            "note": "Карточка с такими данными уже привязана к другому аккаунту.",
                        }

            preloaded = [r for r in preloaded if int(r.get("id") or 0) != source_id]

            mergeable: list[dict] = []
            mismatch_inn = False
            mismatch_phone = False

            for r in preloaded:
                rin = _norm_inn(r.get("inn"))
                rph = _norm_phone(r.get("phone"))

                match_inn = (rin == inn_n) if rin else False
                match_phone = (rph == phone_n) if rph else False

                if match_inn and match_phone:
                    mergeable.append(r)
                    continue
                if match_phone and not rin:
                    mergeable.append(r)
                    continue
                if match_inn and not rph:
                    mergeable.append(r)
                    continue
                if match_inn and (not match_phone):
                    mismatch_inn = True
                if match_phone and (not match_inn):
                    mismatch_phone = True

            seen_ids = set()
            uniq_mergeable = []
            for r in mergeable:
                rid = int(r.get("id") or 0)
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    uniq_mergeable.append(r)
            mergeable = uniq_mergeable

            if len(mergeable) > 1:
                await _maybe_await(conn.commit())
                return {
                    "status": "ambiguous",
                    "merged": False,
                    "target_id": None,
                    "note": "Найдено несколько карточек клиента, требуется ручная проверка.",
                }

            if len(mergeable) == 1:
                target = mergeable[0]
                target_id = int(target.get("id") or 0)
                if not target_id or target_id == source_id:
                    await _maybe_await(conn.commit())
                    return {"status": "already", "merged": False, "target_id": target_id, "note": None}

                # Освобождаем уникальный max_user_id
                await c.execute(
                    "UPDATE users SET max_user_id=NULL, updated_at=NOW() WHERE id=%s AND max_user_id=%s",
                    (source_id, max_uid),
                )

                # Переносим ссылки
                await c.execute("UPDATE orders SET user_id=%s WHERE user_id=%s", (target_id, source_id))
                await c.execute("UPDATE codes  SET user_id=%s WHERE user_id=%s", (target_id, source_id))
                await c.execute("UPDATE client_kkt SET client_id=%s WHERE client_id=%s", (target_id, source_id))

                # Объединяем данные
                await c.execute(
                    """
                    UPDATE users
                       SET max_user_id=%s,
                           phone=COALESCE(phone, %s),
                           inn=COALESCE(inn, %s),
                           first_name=COALESCE(first_name, %s),
                           last_name=COALESCE(last_name, %s),
                           email=COALESCE(email, %s),
                           city=COALESCE(city, %s),
                           org_name=COALESCE(org_name, %s),
                           kpp=COALESCE(kpp, %s),
                           consent_accepted = IF(consent_accepted=1 OR %s=1, 1, 0),
                           is_verified = IF(is_verified=1 OR %s=1, 1, 0),
                           updated_at=NOW()
                     WHERE id=%s
                    """,
                    (
                        max_uid,
                        phone_n,
                        inn_n,
                        source.get("first_name"),
                        source.get("last_name"),
                        source.get("email"),
                        source.get("city"),
                        source.get("org_name"),
                        source.get("kpp"),
                        int(bool(source.get("consent_accepted"))),
                        int(bool(source.get("is_verified"))),
                        target_id,
                    ),
                )

                # Удаляем временную запись
                await c.execute("DELETE FROM users WHERE id=%s", (source_id,))

                await _maybe_await(conn.commit())

                # Авто-создание компании из данных контакта
                try:
                    await _auto_create_or_link_company(target_id)
                except Exception:
                    pass  # Не ломаем основной процесс

                return {
                    "status": "merged",
                    "merged": True,
                    "target_id": target_id,
                    "note": "Профиль сведён с карточкой клиента.",
                }

            await _maybe_await(conn.commit())
            if mismatch_inn:
                return {
                    "status": "mismatch_inn",
                    "merged": False,
                    "target_id": None,
                    "note": "В базе найден клиент с таким ИНН, но другим телефоном.",
                }
            if mismatch_phone:
                return {
                    "status": "mismatch_phone",
                    "merged": False,
                    "target_id": None,
                    "note": "В базе найден клиент с таким телефоном, но другим ИНН.",
                }

            return {"status": "no_match", "merged": False, "target_id": None, "note": None}


# =========================================================
# Авто-создание / привязка компании по данным контакта
# =========================================================
async def _auto_create_or_link_company(user_id: int) -> None:
    """
    Если у пользователя заполнено org_name — автоматически создаёт или находит
    компанию в таблице companies и привязывает контакт через company_contacts.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT org_name, inn, kpp, phone, email, city FROM users WHERE id = %s",
                (user_id,),
            )
            row = await c.fetchone()
            if not row:
                return

            org_name = (row.get("org_name") or "").strip()
            if not org_name:
                return

            inn = (row.get("inn") or "").strip() or None
            kpp = (row.get("kpp") or "").strip() or None
            phone = (row.get("phone") or "").strip() or None
            email = (row.get("email") or "").strip() or None
            city = (row.get("city") or "").strip() or None

            # Гарантируем существование таблиц
            await c.execute("""
                CREATE TABLE IF NOT EXISTS companies (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    inn VARCHAR(20) DEFAULT NULL,
                    kpp VARCHAR(20) DEFAULT NULL,
                    ogrn VARCHAR(20) DEFAULT NULL,
                    address TEXT DEFAULT NULL,
                    phone VARCHAR(50) DEFAULT NULL,
                    email VARCHAR(255) DEFAULT NULL,
                    website VARCHAR(255) DEFAULT NULL,
                    description TEXT DEFAULT NULL,
                    added_by_specialist_id INT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    KEY idx_inn (inn),
                    KEY idx_name (name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS company_contacts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    contact_id INT NOT NULL,
                    role VARCHAR(100) DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_company (company_id),
                    KEY idx_contact (contact_id),
                    UNIQUE KEY unique_company_contact (company_id, contact_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            company_id = None

            # Ищем по ИНН
            if inn:
                await c.execute("SELECT id FROM companies WHERE inn = %s LIMIT 1", (inn,))
                match = await c.fetchone()
                if match:
                    company_id = match["id"] if isinstance(match, dict) else match[0]

            # Ищем по названию
            if company_id is None:
                await c.execute("SELECT id FROM companies WHERE name = %s LIMIT 1", (org_name,))
                match = await c.fetchone()
                if match:
                    company_id = match["id"] if isinstance(match, dict) else match[0]

            if company_id is None:
                # Создаём новую компанию
                await c.execute("""
                    INSERT INTO companies (name, inn, kpp, phone, email, address)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (org_name, inn, kpp, phone, email, city))
                await c.execute("SELECT LAST_INSERT_ID()")
                lid = await c.fetchone()
                company_id = lid["LAST_INSERT_ID()"] if isinstance(lid, dict) else lid[0]
            else:
                # Дополняем пустые поля
                updates = []
                params = []
                if inn:
                    updates.append("inn = COALESCE(inn, %s)")
                    params.append(inn)
                if kpp:
                    updates.append("kpp = COALESCE(kpp, %s)")
                    params.append(kpp)
                if phone:
                    updates.append("phone = COALESCE(phone, %s)")
                    params.append(phone)
                if email:
                    updates.append("email = COALESCE(email, %s)")
                    params.append(email)
                if city:
                    updates.append("address = COALESCE(address, %s)")
                    params.append(city)
                if updates:
                    params.append(company_id)
                    await c.execute(
                        f"UPDATE companies SET {', '.join(updates)}, updated_at = NOW() WHERE id = %s",
                        tuple(params),
                    )

            # Привязываем контакт
            await c.execute("""
                INSERT IGNORE INTO company_contacts (company_id, contact_id)
                VALUES (%s, %s)
            """, (company_id, user_id))

            await _maybe_await(conn.commit())


# =========================================================
# client_kkt slots (ОФД / ФН / Маркировка хранятся прямо в client_kkt)
# =========================================================
async def _resolve_user_id(user_id_or_inn: Any) -> Optional[int]:
    """
    Резолвим внутренний users.id из:
    - tg_user_id (telegram id)
    - inn
    - внутренний id (если уже int и есть в users)
    """
    if user_id_or_inn is None:
        return None

    from db.users import resolve_internal_user_id

    # Пробуем как число (может быть tg_user_id или внутренний id)
    try:
        num = int(str(user_id_or_inn))
        if num > 0:
            resolved = await resolve_internal_user_id(tg_user_id=num)
            if resolved:
                return int(resolved)
            resolved = await resolve_internal_user_id(max_user_id=num)
            if resolved:
                return int(resolved)
            resolved = await resolve_internal_user_id(internal_user_id=num)
            if resolved:
                return int(resolved)
    except Exception:
        pass

    # Иначе считаем, что это inn
    cli = await get_client_by_inn(str(user_id_or_inn))
    return int(cli["id"]) if cli and cli.get("id") is not None else None


# Алиас для обратной совместимости
async def _resolve_client_id(client_id_or_inn: Any) -> Optional[int]:
    return await _resolve_user_id(client_id_or_inn)


async def get_client_ofd_slots(client_id_or_inn: Any) -> List[Dict[str, Any]]:
    """
    Получить OFD сроки пользователя.
    Данные хранятся прямо в client_kkt (fn/ofd/mark_expires_at).
    """
    uid = await _resolve_user_id(client_id_or_inn)
    if not uid:
        return []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("""
                SELECT id, kkt_slot, ofd_expires_at
                FROM client_kkt
                WHERE client_id=%s AND ofd_expires_at IS NOT NULL
                ORDER BY id
            """, (uid,))
            return await c.fetchall()


async def get_client_fn_slots(client_id_or_inn: Any) -> List[Dict[str, Any]]:
    """
    Получить FN сроки пользователя.
    Данные хранятся прямо в client_kkt.
    """
    uid = await _resolve_user_id(client_id_or_inn)
    if not uid:
        return []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("""
                SELECT id, kkt_slot, fn_expires_at
                FROM client_kkt
                WHERE client_id=%s AND fn_expires_at IS NOT NULL
                ORDER BY id
            """, (uid,))
            return await c.fetchall()


async def get_client_mark_slots(client_id_or_inn: Any) -> List[Dict[str, Any]]:
    """
    Получить сроки маркировки пользователя.
    Данные хранятся прямо в client_kkt.
    """
    uid = await _resolve_user_id(client_id_or_inn)
    if not uid:
        return []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("""
                SELECT id, kkt_slot, mark_expires_at
                FROM client_kkt
                WHERE client_id=%s AND mark_expires_at IS NOT NULL
                ORDER BY id
            """, (uid,))
            return await c.fetchall()


async def get_client_kkt_slots(client_id_or_inn: Any) -> List[Dict[str, Any]]:
    """
    Получить KKT слоты пользователя.
    В новой схеме: client_kkt.client_id - это FK на users.id.
    """
    uid = await _resolve_user_id(client_id_or_inn)
    if not uid:
        return []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT * FROM client_kkt WHERE client_id=%s ORDER BY id", (uid,))
            return await c.fetchall()


def _normalize_phone_digits(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 10 and digits.startswith('9'):
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits


async def get_company_owner_kkt_slots_by_inn(
    company_inn: str,
    profile_phone: Optional[str] = None,
) -> tuple[Optional[int], List[Dict[str, Any]]]:
    """
    По ИНН компании и телефону профиля найти contact-owner и его KKT.
    Доступ только при одновременном совпадении ИНН и телефона контакта.
    """
    inn = ''.join(ch for ch in str(company_inn or '') if ch.isdigit())
    if not inn:
        return None, []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute("SELECT id, inn, phone FROM companies WHERE inn=%s ORDER BY id LIMIT 1", (inn,))
            company = await c.fetchone()
            if not company:
                return None, []
            company_id = int(company["id"])
            company_inn_norm = ''.join(ch for ch in str(company.get("inn") or '') if ch.isdigit())
            profile_phone_digits = _normalize_phone_digits(profile_phone)
            if not company_inn_norm or not profile_phone_digits:
                return None, []
            await c.execute("""
                SELECT cc.contact_id, cc.role, u.inn, u.phone
                FROM company_contacts cc
                JOIN users u ON u.id = cc.contact_id
                WHERE cc.company_id=%s
            """, (company_id,))
            contacts = await c.fetchall() or []
            matched = []
            for row in contacts:
                contact_inn = ''.join(ch for ch in str(row.get("inn") or '') if ch.isdigit())
                contact_phone_digits = _normalize_phone_digits(row.get("phone"))
                if contact_inn == company_inn_norm and contact_phone_digits == profile_phone_digits:
                    matched.append(int(row["contact_id"]))
            matched = sorted(set(matched))
            if len(matched) != 1:
                return None, []
            owner_id = matched[0]
            # Ищем ККТ и по company_id (кассы добавленные через карточку компании),
            # и по client_id без company_id (старые записи добавленные через клиента)
            await c.execute(
                "SELECT * FROM client_kkt WHERE company_id=%s OR (client_id=%s AND company_id IS NULL) ORDER BY id",
                (company_id, owner_id),
            )
            return owner_id, (await c.fetchall() or [])


async def get_kkt_slots_by_client_id(client_id: int) -> List[Dict[str, Any]]:
    """Все ККТ напрямую по client_id (users.id). Используется в профиле бота."""
    if not client_id:
        return []
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT * FROM client_kkt WHERE client_id=%s ORDER BY id",
                (int(client_id),),
            )
            return await c.fetchall() or []


async def get_all_clients_with_ofd() -> List[Dict[str, Any]]:
    """
    Для рассылок: users -> client_kkt (ofd_expires_at).
    Возвращает:
    [
      {"user_id": "...", "inn": "...", "name": "...", "ofd_slots":[{"kkt_slot": "...", "ofd_expires_at": "..."}]}
    ]
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  u.tg_user_id     AS user_id,
                  u.inn            AS inn,
                  u.first_name     AS name,
                  k.kkt_slot       AS kkt_slot,
                  k.ofd_expires_at AS ofd_expires_at
                FROM users u
                JOIN client_kkt k ON k.client_id = u.id
                WHERE u.tg_user_id IS NOT NULL
                  AND k.ofd_expires_at IS NOT NULL
                ORDER BY u.tg_user_id, k.kkt_slot
                """
            )
            rows = await c.fetchall() or []

    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        uid = str(r.get("user_id") or "").strip()
        if not uid:
            continue

        g = grouped.setdefault(uid, {
            "user_id": uid,
            "inn": (r.get("inn") or ""),
            "name": (r.get("name") or ""),
            "ofd_slots": [],
        })
        g["ofd_slots"].append({
            "kkt_slot": (r.get("kkt_slot") or "тт.1"),
            "ofd_expires_at": r.get("ofd_expires_at"),
        })

    return list(grouped.values())


async def get_all_clients_with_ofd_max() -> List[Dict[str, Any]]:
    """
    Аналог get_all_clients_with_ofd, но для MAX-пользователей.
    Возвращает список с max_user_id вместо tg_user_id.
    """
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                """
                SELECT
                  u.max_user_id    AS user_id,
                  u.id             AS internal_id,
                  u.inn            AS inn,
                  u.first_name     AS name,
                  k.kkt_slot       AS kkt_slot,
                  k.ofd_expires_at AS ofd_expires_at
                FROM users u
                JOIN client_kkt k ON k.client_id = u.id
                WHERE u.max_user_id IS NOT NULL
                  AND k.ofd_expires_at IS NOT NULL
                ORDER BY u.max_user_id, k.kkt_slot
                """
            )
            rows = await c.fetchall() or []

    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        uid = str(r.get("user_id") or "").strip()
        if not uid:
            continue

        g = grouped.setdefault(uid, {
            "user_id": uid,
            "internal_id": r.get("internal_id"),
            "inn": (r.get("inn") or ""),
            "name": (r.get("name") or ""),
            "ofd_slots": [],
        })
        g["ofd_slots"].append({
            "kkt_slot": (r.get("kkt_slot") or "тт.1"),
            "ofd_expires_at": r.get("ofd_expires_at"),
        })

    return list(grouped.values())


async def search_clients_by_inn_phone_name(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Поиск клиентов по ИНН, телефону или имени.
    Возвращает список пользователей, соответствующих запросу.
    
    Args:
        query: Строка поиска (может быть ИНН, телефон или имя)
        limit: Максимальное количество результатов
    
    Returns:
        Список словарей с данными клиентов
    """
    if not query or len(query.strip()) < 3:
        return []
    
    query = query.strip()
    
    # Нормализуем для поиска по ИНН и телефону
    inn_normalized = _norm_inn(query)
    phone_normalized = _norm_phone(query)
    phone_last10 = _phone_last10(query)
    
    # Для поиска по имени используем LIKE с wildcards
    name_pattern = f"%{query}%"
    
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            # Строим запрос с несколькими условиями поиска
            conditions = []
            params = []
            
            # Поиск по ИНН (точное или частичное совпадение)
            if inn_normalized:
                conditions.append("u.inn LIKE %s")
                params.append(f"%{inn_normalized}%")
            
            # Поиск по телефону (последние 10 цифр)
            if phone_last10:
                conditions.append("""
                    RIGHT(
                        REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(u.phone,'+',''),'-',''),' ',''),'(',''),')',''),'.',''),
                        10
                    ) LIKE %s
                """)
                params.append(f"%{phone_last10}%")
            
            # Поиск по имени или фамилии
            conditions.append("(u.first_name LIKE %s OR u.last_name LIKE %s)")
            params.append(name_pattern)
            params.append(name_pattern)
            
            # Поиск по названию организации
            conditions.append("u.org_name LIKE %s")
            params.append(name_pattern)
            
            where_clause = " OR ".join(conditions)
            params.append(limit)
            
            await c.execute(
                f"""
                SELECT u.*,
                       EXISTS(SELECT 1 FROM client_kkt ck WHERE ck.client_id=u.id LIMIT 1) AS has_kkt
                  FROM users u
                 WHERE {where_clause}
                 ORDER BY
                       has_kkt DESC,
                       (u.inn IS NOT NULL AND u.inn <> '') DESC,
                       (u.tg_user_id IS NOT NULL OR u.max_user_id IS NOT NULL) DESC,
                       u.id DESC
                 LIMIT %s
                """,
                tuple(params),
            )
            rows = await c.fetchall() or []
            
            # Добавляем client_id для обратной совместимости
            for row in rows:
                row["client_id"] = row.get("id")
            
            return rows
