"""Checks resource (section 4 of the API documentation)."""
from __future__ import annotations

from ..exceptions import UnacceptableCompanyError
from ..models.checks import DuplicateCheckRequest, DuplicateCheckResult
from ..models.common import ApiError
from ..transport import AsyncTransport, SyncTransport


class ChecksResource:
    """Synchronous duplicate-check endpoint."""

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def check_duplicate(self, request: DuplicateCheckRequest) -> DuplicateCheckResult:
        """``POST /checks`` — check whether a lead is a duplicate.

        Convention from the spec:
            * 200 OK → not a duplicate, ``DuplicateCheckResult(is_duplicate=False)``.
            * 403 ``UNACCEPTABLE_COMPANY`` → duplicate, returned as a result rather
              than raised (so the caller can iterate over many checks without ``try``).
            * Any other 4xx/5xx → original exception is raised as usual.
        """
        try:
            self._transport.request(
                "POST",
                "/checks",
                response_model=None,
                json_body=request.to_api_dict(),
            )
        except UnacceptableCompanyError as exc:
            return DuplicateCheckResult(
                is_duplicate=True,
                error=ApiError(
                    status=exc.status_code,
                    code=exc.error_code,
                    title=exc.title,
                    detail=exc.detail,
                ),
            )
        return DuplicateCheckResult(is_duplicate=False, error=None)


class AsyncChecksResource:
    """Asynchronous duplicate-check endpoint."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def check_duplicate(self, request: DuplicateCheckRequest) -> DuplicateCheckResult:
        try:
            await self._transport.request(
                "POST",
                "/checks",
                response_model=None,
                json_body=request.to_api_dict(),
            )
        except UnacceptableCompanyError as exc:
            return DuplicateCheckResult(
                is_duplicate=True,
                error=ApiError(
                    status=exc.status_code,
                    code=exc.error_code,
                    title=exc.title,
                    detail=exc.detail,
                ),
            )
        return DuplicateCheckResult(is_duplicate=False, error=None)
