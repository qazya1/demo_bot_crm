"""Scoring resource (section 5 of the API documentation)."""
from __future__ import annotations

import asyncio
import time
from typing import cast

from ..enums import ScoringDecision
from ..exceptions import ScoringTimeoutError
from ..models.scoring import (
    ScoringRequest,
    ScoringResult,
    ScoringSubmitResponse,
)
from ..transport import AsyncTransport, SyncTransport

_FINAL_DECISIONS: frozenset[str] = frozenset(
    {ScoringDecision.ACCEPT.value, ScoringDecision.DECLINE.value}
)

_DEFAULT_MAX_ATTEMPTS = 10
_DEFAULT_INTERVAL = 30.0


def _is_final(result: ScoringResult) -> bool:
    raw = result.decision.value if isinstance(result.decision, ScoringDecision) else result.decision
    return raw in _FINAL_DECISIONS


class ScoringResource:
    """Synchronous scoring endpoints."""

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def submit(self, request: ScoringRequest) -> ScoringSubmitResponse:
        """``POST /scoring`` — submit a scoring request."""
        result = self._transport.request(
            "POST",
            "/scoring",
            response_model=ScoringSubmitResponse,
            json_body=request.to_api_dict(),
        )
        return cast(ScoringSubmitResponse, result)

    def get_result(self, request_id: str) -> ScoringResult:
        """``GET /scoring/{id}`` — fetch the current scoring result."""
        result = self._transport.request(
            "GET",
            f"/scoring/{request_id}",
            response_model=ScoringResult,
        )
        return cast(ScoringResult, result)

    def wait_for_result(
        self,
        request_id: str,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        interval: float = _DEFAULT_INTERVAL,
        *,
        sleep: "callable[[float], None] | None" = None,  # type: ignore[name-defined]
    ) -> ScoringResult:
        """Poll ``get_result`` until the decision is final.

        Args:
            request_id: id returned by ``submit``.
            max_attempts: total number of polling attempts.
            interval: seconds between attempts.
            sleep: optional sleep function override (for testing).

        Raises:
            ScoringTimeoutError: when the decision is still not final after
                ``max_attempts`` attempts.
        """
        sleep_fn = sleep or time.sleep
        result: ScoringResult | None = None
        for attempt in range(1, max_attempts + 1):
            result = self.get_result(request_id)
            if _is_final(result):
                return result
            if attempt < max_attempts:
                sleep_fn(interval)
        raise ScoringTimeoutError(request_id=request_id, attempts=max_attempts)


class AsyncScoringResource:
    """Asynchronous scoring endpoints."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def submit(self, request: ScoringRequest) -> ScoringSubmitResponse:
        result = await self._transport.request(
            "POST",
            "/scoring",
            response_model=ScoringSubmitResponse,
            json_body=request.to_api_dict(),
        )
        return cast(ScoringSubmitResponse, result)

    async def get_result(self, request_id: str) -> ScoringResult:
        result = await self._transport.request(
            "GET",
            f"/scoring/{request_id}",
            response_model=ScoringResult,
        )
        return cast(ScoringResult, result)

    async def wait_for_result(
        self,
        request_id: str,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        interval: float = _DEFAULT_INTERVAL,
    ) -> ScoringResult:
        for attempt in range(1, max_attempts + 1):
            result = await self.get_result(request_id)
            if _is_final(result):
                return result
            if attempt < max_attempts:
                await asyncio.sleep(interval)
        raise ScoringTimeoutError(request_id=request_id, attempts=max_attempts)
