"""Dictionaries resource (section 6 of the API documentation)."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from ..models.dictionaries import Branch, City, Region
from ..transport import AsyncTransport, SyncTransport, _build_headers, _build_url
from ..transport import _execute_async, _execute_sync, _extract_error


class DictionaryCache:
    """In-memory TTL cache for dictionary calls.

    Thread-safe for sync usage; the async resource uses an ``asyncio.Lock`` per
    instance to avoid double-fetching. ``ttl=None`` disables caching.
    """

    def __init__(self, ttl: float | None = 24 * 60 * 60) -> None:
        self.ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None

    def get(self, key: str) -> Any | None:
        if self.ttl is None:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self.ttl:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        if self.ttl is None:
            return
        with self._lock:
            self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def _ensure_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock


def _parse_list(payload: Any, model_cls: type) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("items", "data"):
            if key in payload and isinstance(payload[key], list):
                items = payload[key]
                break
        else:
            items = []
    else:
        items = []
    return [model_cls.model_validate(item) for item in items]


class DictionariesResource:
    """Synchronous dictionary endpoints."""

    def __init__(
        self,
        transport: SyncTransport,
        cache: DictionaryCache | None = None,
    ) -> None:
        self._transport = transport
        self._cache = cache

    def _fetch(self, path: str, params: dict[str, Any] | None) -> Any:
        url = _build_url(self._transport.config, path)
        headers = _build_headers(self._transport.config)
        prepared = _execute_sync(
            self._transport._client, "GET", url, headers, None, params,
            self._transport.config, self._transport.policy,
        )
        if not prepared.is_success():
            raise _extract_error(prepared)
        return prepared.json_body

    def cities(self) -> list[City]:
        """``GET /dictionaries?code=cities``."""
        cache_key = "cities"
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit  # type: ignore[no-any-return]
        body = self._fetch("/dictionaries", {"code": "cities"})
        result = _parse_list(body, City)
        if self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    def regions(self) -> list[Region]:
        """``GET /dictionaries?code=regions``."""
        cache_key = "regions"
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit  # type: ignore[no-any-return]
        body = self._fetch("/dictionaries", {"code": "regions"})
        result = _parse_list(body, Region)
        if self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    def branches(self, city_fias_id: str) -> list[Branch]:
        """``GET /branches/{fiasId}``."""
        cache_key = f"branches:{city_fias_id}"
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit  # type: ignore[no-any-return]
        body = self._fetch(f"/branches/{city_fias_id}", None)
        result = _parse_list(body, Branch)
        if self._cache is not None:
            self._cache.set(cache_key, result)
        return result


class AsyncDictionariesResource:
    """Asynchronous dictionary endpoints."""

    def __init__(
        self,
        transport: AsyncTransport,
        cache: DictionaryCache | None = None,
    ) -> None:
        self._transport = transport
        self._cache = cache

    async def _fetch(self, path: str, params: dict[str, Any] | None) -> Any:
        url = _build_url(self._transport.config, path)
        headers = _build_headers(self._transport.config)
        prepared = await _execute_async(
            self._transport._client, "GET", url, headers, None, params,
            self._transport.config, self._transport.policy,
        )
        if not prepared.is_success():
            raise _extract_error(prepared)
        return prepared.json_body

    async def cities(self) -> list[City]:
        cache_key = "cities"
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit  # type: ignore[no-any-return]
        body = await self._fetch("/dictionaries", {"code": "cities"})
        result = _parse_list(body, City)
        if self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    async def regions(self) -> list[Region]:
        cache_key = "regions"
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit  # type: ignore[no-any-return]
        body = await self._fetch("/dictionaries", {"code": "regions"})
        result = _parse_list(body, Region)
        if self._cache is not None:
            self._cache.set(cache_key, result)
        return result

    async def branches(self, city_fias_id: str) -> list[Branch]:
        cache_key = f"branches:{city_fias_id}"
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit  # type: ignore[no-any-return]
        body = await self._fetch(f"/branches/{city_fias_id}", None)
        result = _parse_list(body, Branch)
        if self._cache is not None:
            self._cache.set(cache_key, result)
        return result
