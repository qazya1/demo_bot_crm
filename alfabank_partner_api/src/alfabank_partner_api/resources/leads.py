"""Leads resource (sections 2, 3.1, 3.2 of the API documentation)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, cast

from ..models.leads import (
    LeadCreateRequest,
    LeadCreateResponse,
    LeadInfo,
    LeadSummary,
)
from ..transport import AsyncTransport, SyncTransport

_FilterDateBy = Literal["modifiedAt", "createdAt"]

_MAX_PER_PAGE = 100


def _to_unix_ms(dt: datetime) -> int:
    """Convert a datetime to unix epoch ms.

    Naive datetimes are assumed to be UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _build_list_params(
    page: int,
    per_page: int,
    from_date: datetime | None,
    to_date: datetime | None,
    filter_date_by: _FilterDateBy | None,
) -> dict[str, Any]:
    if per_page < 1 or per_page > _MAX_PER_PAGE:
        raise ValueError(f"per_page must be in [1, {_MAX_PER_PAGE}]")
    if page < 1:
        raise ValueError("page must be >= 1")
    params: dict[str, Any] = {"page": page, "perPage": per_page}
    if from_date is not None:
        params["fromDate"] = _to_unix_ms(from_date)
    if to_date is not None:
        params["toDate"] = _to_unix_ms(to_date)
    if filter_date_by is not None:
        params["filterDateBy"] = filter_date_by
    return params


def _parse_summary_list(payload: Any) -> list[LeadSummary]:
    if payload is None:
        return []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("items", "data", "leads"):
            if key in payload and isinstance(payload[key], list):
                items = payload[key]
                break
        else:
            items = []
    else:
        items = []
    return [LeadSummary.model_validate(item) for item in items]


class _LeadsListEnvelope:
    """Internal helper — resource calls ``transport.request`` with a placeholder
    response model and post-processes the body manually because the docs accept
    both a bare list and a wrapped envelope."""


class LeadsResource:
    """Synchronous leads endpoints."""

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def create(self, request: LeadCreateRequest) -> LeadCreateResponse:
        """``POST /leads`` — submit a new lead.

        Args:
            request: Validated ``LeadCreateRequest``.

        Returns:
            ``LeadCreateResponse`` with the generated lead id.
        """
        result = self._transport.request(
            "POST",
            "/leads",
            response_model=LeadCreateResponse,
            json_body=request.to_api_dict(),
        )
        return cast(LeadCreateResponse, result)

    def list(
        self,
        page: int = 1,
        per_page: int = _MAX_PER_PAGE,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        filter_date_by: _FilterDateBy | None = None,
    ) -> list[LeadSummary]:
        """``GET /leads`` — paginated list of leads.

        Args:
            page: 1-based page index.
            per_page: page size, max 100.
            from_date / to_date: filter by ``filter_date_by`` (converted to unix ms).
            filter_date_by: which timestamp to filter by.
        """
        params = _build_list_params(page, per_page, from_date, to_date, filter_date_by)
        # The spec is permissive about the envelope; bypass model-coupled parsing:
        from ..transport import _build_url, _build_headers, _execute_sync, _extract_error  # noqa: PLC0415

        url = _build_url(self._transport.config, "/leads")
        headers = _build_headers(self._transport.config)
        prepared = _execute_sync(
            self._transport._client, "GET", url, headers, None, params,
            self._transport.config, self._transport.policy,
        )
        if not prepared.is_success():
            raise _extract_error(prepared)
        return _parse_summary_list(prepared.json_body)

    def get(self, lead_id: str) -> LeadInfo:
        """``GET /leads/{id}`` — full information about a lead."""
        result = self._transport.request(
            "GET",
            f"/leads/{lead_id}",
            response_model=LeadInfo,
        )
        return cast(LeadInfo, result)


class AsyncLeadsResource:
    """Asynchronous leads endpoints."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def create(self, request: LeadCreateRequest) -> LeadCreateResponse:
        result = await self._transport.request(
            "POST",
            "/leads",
            response_model=LeadCreateResponse,
            json_body=request.to_api_dict(),
        )
        return cast(LeadCreateResponse, result)

    async def list(
        self,
        page: int = 1,
        per_page: int = _MAX_PER_PAGE,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        filter_date_by: _FilterDateBy | None = None,
    ) -> list[LeadSummary]:
        params = _build_list_params(page, per_page, from_date, to_date, filter_date_by)
        from ..transport import _build_url, _build_headers, _execute_async, _extract_error  # noqa: PLC0415

        url = _build_url(self._transport.config, "/leads")
        headers = _build_headers(self._transport.config)
        prepared = await _execute_async(
            self._transport._client, "GET", url, headers, None, params,
            self._transport.config, self._transport.policy,
        )
        if not prepared.is_success():
            raise _extract_error(prepared)
        return _parse_summary_list(prepared.json_body)

    async def get(self, lead_id: str) -> LeadInfo:
        result = await self._transport.request(
            "GET",
            f"/leads/{lead_id}",
            response_model=LeadInfo,
        )
        return cast(LeadInfo, result)
