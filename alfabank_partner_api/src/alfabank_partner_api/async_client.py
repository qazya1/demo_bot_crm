"""Asynchronous Alfa-Bank client."""
from __future__ import annotations

from typing import Any

import httpx

from .config import ClientConfig
from .resources import (
    AsyncChecksResource,
    AsyncDictionariesResource,
    AsyncLeadsResource,
    AsyncScoringResource,
    DictionaryCache,
)
from .transport import AsyncTransport, RetryPolicy


class AlfaBankAsyncClient:
    """Asynchronous client for the Alfa-Bank partner API.

    Example:
        >>> from alfabank_partner_api import AlfaBankAsyncClient, ClientConfig
        >>> async with AlfaBankAsyncClient(config=ClientConfig(api_key="...")) as client:
        ...     lead = await client.leads.create(request)
    """

    def __init__(
        self,
        config: ClientConfig,
        *,
        retry_policy: RetryPolicy | None = None,
        dictionary_cache: DictionaryCache | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._transport = AsyncTransport(
            config, retry_policy=retry_policy, client=http_client,
        )
        self.leads = AsyncLeadsResource(self._transport)
        self.checks = AsyncChecksResource(self._transport)
        self.scoring = AsyncScoringResource(self._transport)
        self.dictionaries = AsyncDictionariesResource(
            self._transport, cache=dictionary_cache,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> "AlfaBankAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()
