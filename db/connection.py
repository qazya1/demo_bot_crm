"""
Пул соединений MySQL + вспомогательные функции.
Все остальные модули db.* импортируют из этого файла.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from mysql.connector.aio import connect
from mysql.connector.errors import (
    DatabaseError,
    IntegrityError,
    InterfaceError,
    OperationalError,
)

load_dotenv(Path(__file__).resolve().parent.parent / "inf.env")

# =========================================================
# MySQL config — все значения из .env
# =========================================================
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DB", "bot_db"),
    "autocommit": False,
    "use_pure": True,
    "ssl_disabled": True,
}

_POOL_SIZE = int(os.getenv("MYSQL_POOL_SIZE", "10"))
USE_TEST_TABLES = os.getenv("DB_USE_TEST_TABLES", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TEST_TABLE_SUFFIX = os.getenv("DB_TEST_TABLE_SUFFIX", "_test")

_SYSTEM_TABLES = {"information_schema", "mysql", "performance_schema", "sys", "dual"}
_TABLE_IDENT = r"`?[A-Za-z_][A-Za-z0-9_]*`?(?:\.`?[A-Za-z_][A-Za-z0-9_]*`?)?"
_SQL_TABLE_PATTERNS = (
    re.compile(rf"(?P<prefix>\bFROM\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(rf"(?P<prefix>\bJOIN\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(rf"(?P<prefix>\bINTO\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(rf"(?P<prefix>(?:^|;)\s*UPDATE\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(rf"(?P<prefix>(?:^|;)\s*DELETE\s+FROM\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(rf"(?P<prefix>(?:^|;)\s*ALTER\s+TABLE\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(rf"(?P<prefix>(?:^|;)\s*TRUNCATE\s+TABLE\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(rf"(?P<prefix>(?:^|;)\s*DESCRIBE\s+)(?P<ident>{_TABLE_IDENT})", re.IGNORECASE),
    re.compile(
        rf"(?P<prefix>(?:^|;)\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(?P<ident>{_TABLE_IDENT})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<prefix>(?:^|;)\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?)(?P<ident>{_TABLE_IDENT})",
        re.IGNORECASE,
    ),
)


def _rewrite_table_name_for_test(name: str) -> str:
    ident = name.strip()
    if "." in ident:
        return name
    has_backticks = ident.startswith("`") and ident.endswith("`")
    clean = ident[1:-1] if has_backticks else ident
    clean_l = clean.lower()

    if clean_l in _SYSTEM_TABLES:
        return name
    if clean_l.endswith(TEST_TABLE_SUFFIX.lower()):
        return name

    rewritten = f"{clean}{TEST_TABLE_SUFFIX}"
    return f"`{rewritten}`" if has_backticks else rewritten


def _effective_table_name(table: str) -> str:
    if not USE_TEST_TABLES or not TEST_TABLE_SUFFIX:
        return table
    clean = table.strip().strip("`")
    if not clean:
        return table
    if clean.lower() in _SYSTEM_TABLES:
        return clean
    if clean.lower().endswith(TEST_TABLE_SUFFIX.lower()):
        return clean
    return f"{clean}{TEST_TABLE_SUFFIX}"


def _rewrite_sql_for_test_contour(sql: str) -> str:
    if not USE_TEST_TABLES or not isinstance(sql, str) or not sql.strip():
        return sql
    if not TEST_TABLE_SUFFIX:
        return sql

    def repl(match: re.Match) -> str:
        prefix = match.group("prefix")
        ident = match.group("ident")
        return f"{prefix}{_rewrite_table_name_for_test(ident)}"

    rewritten = sql
    for pattern in _SQL_TABLE_PATTERNS:
        rewritten = pattern.sub(repl, rewritten)
    return rewritten


def _apply_django_db_fallback(config: dict) -> dict:
    """Если env для db-layer не задан, пробуем взять креды из settings.py (Django)."""
    try:
        import settings as django_settings  # локальный settings.py проекта
        default_db = getattr(django_settings, "DATABASES", {}).get("default", {}) or {}
    except Exception:
        return config

    env_user = os.getenv("MYSQL_USER")
    env_pass = os.getenv("MYSQL_PASSWORD")
    env_host = os.getenv("MYSQL_HOST")
    env_name = os.getenv("MYSQL_DB")
    env_port = os.getenv("MYSQL_PORT")

    # подставляем только если соответствующая env-переменная отсутствует
    if not env_user and default_db.get("USER"):
        config["user"] = default_db.get("USER")
    if not env_pass and default_db.get("PASSWORD"):
        config["password"] = default_db.get("PASSWORD")
    if not env_host and default_db.get("HOST"):
        config["host"] = default_db.get("HOST")
    if not env_name and default_db.get("NAME"):
        config["database"] = default_db.get("NAME")
    if not env_port and default_db.get("PORT"):
        try:
            config["port"] = int(default_db.get("PORT"))
        except Exception:
            pass

    return config

MYSQL_CONFIG = _apply_django_db_fallback(MYSQL_CONFIG)


# =========================================================
# Внутренние хелперы пула
# =========================================================
async def _rollback_if_needed(conn) -> None:
    """Если conn пришёл из пула с открытой транзакцией — откатим."""
    try:
        in_tx = bool(getattr(conn, "in_transaction", False))
    except Exception:
        in_tx = False
    if in_tx:
        try:
            await _maybe_await(conn.rollback())
        except Exception:
            pass


async def _begin_tx(conn) -> None:
    """Безопасный старт транзакции."""
    await _rollback_if_needed(conn)
    try:
        await _maybe_await(conn.start_transaction())
    except Exception as e:
        if "Transaction already in progress" in str(e):
            return
        raise


async def _maybe_await(v):
    """Страховка на случай, если метод sync/async."""
    if inspect.isawaitable(v):
        return await v
    return v


def _is_disconnect_error(e: Exception) -> bool:
    s = str(e).lower()
    return (
        "4031" in s
        or "disconnected by the server" in s
        or "because of inactivity" in s
        or "wait_timeout" in s
        or "interactive_timeout" in s
        or "lost connection" in s
        or "server has gone away" in s
    )


async def _ensure_alive(conn) -> bool:
    """Гарантируем, что conn живой."""
    if conn is None:
        return False
    try:
        if hasattr(conn, "ping"):
            await _maybe_await(conn.ping(reconnect=True, attempts=1, delay=0))
            return True
    except Exception as e:
        if _is_disconnect_error(e):
            return False
        return False
    try:
        if hasattr(conn, "is_connected"):
            return bool(await _maybe_await(conn.is_connected()))
    except Exception:
        return False
    return True


# =========================================================
# Пул соединений
# =========================================================
class _AsyncConnPool:
    def __init__(self, maxsize: int):
        self._maxsize = maxsize
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._created = 0
        self._lock = asyncio.Lock()

    async def _new_conn(self):
        return await connect(**MYSQL_CONFIG)

    async def acquire(self):
        cnx = None
        try:
            cnx = self._q.get_nowait()
        except asyncio.QueueEmpty:
            cnx = None

        if cnx is None:
            async with self._lock:
                if self._created < self._maxsize:
                    self._created += 1
                    cnx = await self._new_conn()
            if cnx is None:
                cnx = await self._q.get()

        if not await _ensure_alive(cnx):
            try:
                await _maybe_await(cnx.close())
            except Exception:
                pass
            async with self._lock:
                self._created = max(0, self._created - 1)
            async with self._lock:
                self._created += 1
            cnx = await self._new_conn()

        return cnx

    async def release(self, cnx):
        try:
            if not await _ensure_alive(cnx):
                raise RuntimeError("mysql connection is not connected")
            self._q.put_nowait(cnx)
        except Exception:
            try:
                await _maybe_await(cnx.close())
            except Exception:
                pass
            async with self._lock:
                self._created = max(0, self._created - 1)


_ASYNC_POOL: Optional[_AsyncConnPool] = None


def _get_async_pool() -> _AsyncConnPool:
    global _ASYNC_POOL
    if _ASYNC_POOL is None:
        _ASYNC_POOL = _AsyncConnPool(maxsize=_POOL_SIZE)
    return _ASYNC_POOL


@asynccontextmanager
async def get_conn():
    POOL = _get_async_pool()
    conn = await POOL.acquire()
    try:
        if not await _ensure_alive(conn):
            raise RuntimeError("mysql connection is not alive")
        await _rollback_if_needed(conn)
        yield conn
    finally:
        try:
            await _rollback_if_needed(conn)
        except Exception:
            pass
        await POOL.release(conn)


class _CursorProxy:
    def __init__(self, cursor):
        self._cursor = cursor

    async def execute(self, operation, params=None, *args, **kwargs):
        sql = _rewrite_sql_for_test_contour(operation)
        return await _maybe_await(self._cursor.execute(sql, params, *args, **kwargs))

    async def executemany(self, operation, seq_params, *args, **kwargs):
        sql = _rewrite_sql_for_test_contour(operation)
        return await _maybe_await(self._cursor.executemany(sql, seq_params, *args, **kwargs))

    async def __aenter__(self):
        entered = await _maybe_await(self._cursor.__aenter__())
        return _CursorProxy(entered)

    async def __aexit__(self, exc_type, exc, tb):
        return await _maybe_await(self._cursor.__aexit__(exc_type, exc, tb))

    def __getattr__(self, item):
        return getattr(self._cursor, item)


async def _cur(conn):
    return _CursorProxy(await conn.cursor(dictionary=True))


# =========================================================
# Нормализация / хелперы (используются другими модулями)
# =========================================================
def _norm_inn(inn: str | None) -> str | None:
    if not inn:
        return None
    d = re.sub(r"\D+", "", inn)
    if len(d) in (10, 12):
        return d
    return None


def _norm_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    d = re.sub(r"\D+", "", phone)
    if not d:
        return None
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    if len(d) == 10 and d.startswith("9"):
        d = "7" + d
    if len(d) != 11:
        return None
    return "+{}".format(d)


def _phone_last10(phone: str | None) -> str | None:
    p = _norm_phone(phone)
    if not p:
        return None
    return re.sub(r"\D+", "", p)[-10:]


def _extract_months(text: str | None) -> int | None:
    """Извлекает срок (в месяцах) из строки вида '12 мес', '24 месяца' и т.п."""
    if not text:
        return None
    s = str(text).lower()
    m = re.search(r"(\d{1,2})\s*(?:мес|месяц|месяцев|month|months)", s)
    if not m:
        return None
    try:
        val = int(m.group(1))
        if 1 <= val <= 60:
            return val
    except Exception:
        return None
    return None


def _parse_dt_maybe(v):
    """mysql-connector обычно возвращает datetime, но иногда может прийти str."""
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(v, fmt)
            except Exception:
                pass
    return None


def _add_months_utc(dt: datetime, months: int) -> datetime:
    """Прибавляем N месяцев к datetime (упрощённо)."""
    import calendar
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


async def _table_exists(table: str) -> bool:
    table = _effective_table_name(table)
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s",
                (table,),
            )
            row = await c.fetchone()
            return bool(row and int(row.get("cnt", 0)) > 0)


async def _column_exists(table: str, column: str) -> bool:
    table = _effective_table_name(table)
    async with get_conn() as conn:
        async with await _cur(conn) as c:
            await c.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
                (table, column),
            )
            row = await c.fetchone()
            return bool(row and int(row.get("cnt", 0)) > 0)


async def _index_exists(c, table: str, index_name: str) -> bool:
    table = _effective_table_name(table)
    await c.execute(
        "SELECT COUNT(*) AS cnt FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s",
        (table, index_name),
    )
    row = await c.fetchone()
    return bool(row and int(row.get("cnt", 0)) > 0)


# Токены чата — TTL по умолчанию
TOKEN_TTL_MIN = 30
TOKEN_MAX_STALE_DAYS = int(os.getenv("TOKEN_MAX_STALE_DAYS", "3650"))
