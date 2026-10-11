"""Synchronous Alfa-Bank client."""
from __future__ import annotations

from typing import Any

import httpx

from .config import ClientConfig
from .resources import (
    ChecksResource,
    DictionariesResource,
    DictionaryCache,
    LeadsResource,
    ScoringResource,
)
from .transport import RetryPolicy, SyncTransport


class AlfaBankClient:
    """Synchronous client for the Alfa-Bank partner API.

    Example:
        >>> from alfabank_partner_api import AlfaBankClient, ClientConfig
        >>> with AlfaBankClient(config=ClientConfig(api_key="...")) as client:
        ...     lead = client.leads.create(request)
    """

    def __init__(
        self,
        config: ClientConfig,
        *,
        retry_policy: RetryPolicy | None = None,
        dictionary_cache: DictionaryCache | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._transport = SyncTransport(
            config, retry_policy=retry_policy, client=http_client,
        )
        self.leads = LeadsResource(self._transport)
        self.checks = ChecksResource(self._transport)
        self.scoring = ScoringResource(self._transport)
        self.dictionaries = DictionariesResource(self._transport, cache=dictionary_cache)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "AlfaBankClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
