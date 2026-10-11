"""HTTP transport layer: headers, retries, error mapping, response parsing."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError as PydanticValidationError

from .config import ClientConfig
from .exceptions import (
    AlfaBankAPIError,
    AlfaBankNetworkError,
    AuthenticationError,
    AuthorizationError,
    BlockedPartnerError,
    ChecksLimitError,
    ClientError,
    DuplicateRequestError,
    LeadsLimitError,
    NewCompanyError,
    RateLimitError,
    ServerError,
    ServiceUnavailableError,
    UnacceptableCompanyError,
    ValidationError,
)
from .logging import logger as _module_logger
from .logging import mask_pii, safe_headers
from .models.common import ApiError, ErrorResponse

T = TypeVar("T", bound=BaseModel)

_LOGGER = _module_logger

# Error codes that the API uses (per section 7 of the documentation).
_AUTHORIZATION_CODES: dict[str, type[AuthorizationError]] = {
    "UNACCEPTABLE_COMPANY": UnacceptableCompanyError,
    "BLOCKED_PARTNER": BlockedPartnerError,
    "LEADS_LIMIT_ERROR": LeadsLimitError,
    "CHECKS_LIMIT_ERROR": ChecksLimitError,
    "NEW_COMPANY_ERROR": NewCompanyError,
}

_REPEATED_REQUEST_CODE = "Repeated request error"
_UNEXPECTED_ERROR_CODE = "UNEXPECTED_ERROR"
_VALIDATION_ERROR_CODE = "validationError"
_CLIENT_ERROR_CODE = "CLIENT_ERROR"
_RATE_LIMIT_CODE = "RATE_LIMIT_EXCEEDED"
_SERVICE_UNAVAILABLE_CODE = "SERVICE_UNAVAILABLE"


def _map_error(status: int, error: ApiError) -> AlfaBankAPIError:
    """Map an HTTP status + ``ApiError`` to the right exception subclass."""
    code = error.code
    kwargs: dict[str, Any] = {
        "status_code": status,
        "error_code": code,
        "title": error.title,
        "detail": error.detail,
        "raw_response": error.model_dump(by_alias=True, exclude_none=True),
    }

    if status == 401:
        return AuthenticationError(**kwargs)
    if status == 403:
        return _AUTHORIZATION_CODES.get(code, AuthorizationError)(**kwargs)
    if status == 429:
        return RateLimitError(**kwargs)
    if status == 503:
        return ServiceUnavailableError(**kwargs)
    if status == 500:
        if code == _REPEATED_REQUEST_CODE:
            return DuplicateRequestError(**kwargs)
        return ServerError(**kwargs)
    if status == 400:
        if code == _VALIDATION_ERROR_CODE:
            return ValidationError(**kwargs)
        if code == _CLIENT_ERROR_CODE:
            return ClientError(**kwargs)
    return AlfaBankAPIError(**kwargs)


@dataclass(frozen=True)
class RetryPolicy:
    """Pluggable retry policy.

    Attributes:
        max_retries: Maximum total attempts beyond the initial one.
        backoff_base: Base for exponential backoff (seconds).
        max_500_attempts: Total attempts for HTTP 500 ``UNEXPECTED_ERROR``
            (per section 7 of the documentation: up to 5 attempts at 60s).
        five_hundred_interval: Sleep between 500-UNEXPECTED retries (seconds).
    """

    max_retries: int = 3
    backoff_base: float = 1.0
    max_500_attempts: int = 5
    five_hundred_interval: float = 60.0

    @classmethod
    def from_config(cls, config: ClientConfig) -> "RetryPolicy":
        return cls(
            max_retries=config.max_retries,
            backoff_base=config.retry_backoff_base,
        )

    def backoff(self, attempt: int) -> float:
        """Return seconds to sleep before retry attempt ``attempt`` (1-based)."""
        return self.backoff_base * (2 ** (attempt - 1))


def _is_network_error(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout))


@dataclass
class _PreparedResponse:
    """Internal: a parsed HTTP response, either success or mapped exception."""

    status: int
    json_body: Any
    text: str

    def is_success(self) -> bool:
        return 200 <= self.status < 300


def _parse_body(response: httpx.Response) -> _PreparedResponse:
    text = response.text
    json_body: Any = None
    if text.strip():
        try:
            json_body = response.json()
        except (ValueError, json.JSONDecodeError):
            json_body = None
    return _PreparedResponse(status=response.status_code, json_body=json_body, text=text)


def _extract_error(prepared: _PreparedResponse) -> AlfaBankAPIError:
    """Convert a non-2xx response into an ``AlfaBankAPIError`` subclass."""
    body = prepared.json_body
    if isinstance(body, dict):
        try:
            envelope = ErrorResponse.model_validate(body)
            if envelope.errors:
                return _map_error(prepared.status, envelope.errors[0])
        except PydanticValidationError:
            pass
    fallback = ApiError(
        status=prepared.status,
        code="UNKNOWN",
        title="Unparseable error response",
        detail=prepared.text[:500] or None,
    )
    return _map_error(prepared.status, fallback)


def parse_success(prepared: _PreparedResponse, model_cls: type[T] | None) -> T | None:
    """Parse a 2xx response body into ``model_cls``.

    When ``model_cls`` is None — return None (used by ``check_duplicate`` where
    the body is empty on 200).
    """
    if model_cls is None:
        return None
    body = prepared.json_body
    if body is None:
        if not prepared.text.strip():
            raise AlfaBankAPIError(
                status_code=prepared.status,
                error_code="EMPTY_BODY",
                title="Empty response body",
                detail=None,
                raw_response={},
            )
        raise AlfaBankAPIError(
            status_code=prepared.status,
            error_code="INVALID_JSON",
            title="Response is not valid JSON",
            detail=prepared.text[:500],
            raw_response={},
        )
    try:
        return model_cls.model_validate(body)
    except PydanticValidationError as exc:
        raise AlfaBankAPIError(
            status_code=prepared.status,
            error_code="INVALID_RESPONSE_SCHEMA",
            title="Response did not match expected schema",
            detail=str(exc),
            raw_response=body if isinstance(body, dict) else {"_raw": body},
        ) from exc


def _build_headers(config: ClientConfig) -> dict[str, str]:
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "API-key": config.api_key,
        "User-Agent": config.user_agent,
    }


def _log_request_outgoing(method: str, url: str, headers: dict[str, str], json_body: Any,
                          mask_pii_in_logs: bool) -> None:
    if _LOGGER.isEnabledFor(logging.DEBUG):
        body_for_log = mask_pii(json_body) if (mask_pii_in_logs and json_body is not None) else json_body
        _LOGGER.debug(
            "Outgoing request: %s %s headers=%s body=%s",
            method, url, safe_headers(headers), body_for_log,
        )


def _log_response(method: str, url: str, status: int, latency_ms: float,
                  body: Any, mask_pii_in_logs: bool) -> None:
    _LOGGER.info("Response %s %s -> %s in %.1fms", method, url, status, latency_ms)
    if _LOGGER.isEnabledFor(logging.DEBUG):
        body_for_log = mask_pii(body) if (mask_pii_in_logs and body is not None) else body
        _LOGGER.debug("Response body for %s %s: %s", method, url, body_for_log)


def _build_url(config: ClientConfig, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{config.base_url}{path if path.startswith('/') else '/' + path}"


def _should_retry_status(status: int, error_code: str | None) -> tuple[bool, bool]:
    """Return ``(retryable, is_500_unexpected)``.

    503 → general retry. 500 + UNEXPECTED_ERROR → "slow" retry per documentation.
    Other 4xx/5xx — no retry.
    """
    if status == 503:
        return True, False
    if status == 500 and error_code == _UNEXPECTED_ERROR_CODE:
        return True, True
    return False, False


def _execute_sync(
    client: httpx.Client,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: Any,
    params: dict[str, Any] | None,
    config: ClientConfig,
    policy: RetryPolicy,
) -> _PreparedResponse:
    """Run the request with the retry policy on a sync ``httpx.Client``."""
    network_attempt = 0
    five_hundred_attempt = 0
    while True:
        _log_request_outgoing(method, url, headers, json_body, config.mask_pii_in_logs)
        start = time.monotonic()
        try:
            response = client.request(
                method, url, headers=headers, json=json_body, params=params,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            if network_attempt < policy.max_retries:
                network_attempt += 1
                sleep = policy.backoff(network_attempt)
                _LOGGER.warning(
                    "Network error on %s %s (attempt %d/%d): %r — retrying in %.2fs",
                    method, url, network_attempt, policy.max_retries, exc, sleep,
                )
                time.sleep(sleep)
                continue
            raise AlfaBankNetworkError(
                f"Network error after {policy.max_retries} retries: {exc!r}", cause=exc,
            ) from exc
        latency_ms = (time.monotonic() - start) * 1000
        prepared = _parse_body(response)
        _log_response(method, url, prepared.status, latency_ms, prepared.json_body,
                      config.mask_pii_in_logs)
        if prepared.is_success():
            return prepared

        # Non-2xx → decide whether to retry.
        envelope_code: str | None = None
        if isinstance(prepared.json_body, dict):
            try:
                env = ErrorResponse.model_validate(prepared.json_body)
                if env.errors:
                    envelope_code = env.errors[0].code
            except PydanticValidationError:
                pass

        retryable, is_500_unexpected = _should_retry_status(prepared.status, envelope_code)
        if retryable:
            if is_500_unexpected:
                if five_hundred_attempt + 1 < policy.max_500_attempts:
                    five_hundred_attempt += 1
                    _LOGGER.warning(
                        "500 UNEXPECTED_ERROR on %s %s (attempt %d/%d) — retrying in %.0fs",
                        method, url, five_hundred_attempt, policy.max_500_attempts,
                        policy.five_hundred_interval,
                    )
                    time.sleep(policy.five_hundred_interval)
                    continue
            else:
                if network_attempt < policy.max_retries:
                    network_attempt += 1
                    sleep = policy.backoff(network_attempt)
                    _LOGGER.warning(
                        "503 on %s %s (attempt %d/%d) — retrying in %.2fs",
                        method, url, network_attempt, policy.max_retries, sleep,
                    )
                    time.sleep(sleep)
                    continue
        return prepared


async def _execute_async(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: Any,
    params: dict[str, Any] | None,
    config: ClientConfig,
    policy: RetryPolicy,
) -> _PreparedResponse:
    """Async counterpart to ``_execute_sync``."""
    import asyncio

    network_attempt = 0
    five_hundred_attempt = 0
    while True:
        _log_request_outgoing(method, url, headers, json_body, config.mask_pii_in_logs)
        start = time.monotonic()
        try:
            response = await client.request(
                method, url, headers=headers, json=json_body, params=params,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            if network_attempt < policy.max_retries:
                network_attempt += 1
                sleep = policy.backoff(network_attempt)
                _LOGGER.warning(
                    "Network error on %s %s (attempt %d/%d): %r — retrying in %.2fs",
                    method, url, network_attempt, policy.max_retries, exc, sleep,
                )
                await asyncio.sleep(sleep)
                continue
            raise AlfaBankNetworkError(
                f"Network error after {policy.max_retries} retries: {exc!r}", cause=exc,
            ) from exc
        latency_ms = (time.monotonic() - start) * 1000
        prepared = _parse_body(response)
        _log_response(method, url, prepared.status, latency_ms, prepared.json_body,
                      config.mask_pii_in_logs)
        if prepared.is_success():
            return prepared

        envelope_code: str | None = None
        if isinstance(prepared.json_body, dict):
            try:
                env = ErrorResponse.model_validate(prepared.json_body)
                if env.errors:
                    envelope_code = env.errors[0].code
            except PydanticValidationError:
                pass

        retryable, is_500_unexpected = _should_retry_status(prepared.status, envelope_code)
        if retryable:
            if is_500_unexpected:
                if five_hundred_attempt + 1 < policy.max_500_attempts:
                    five_hundred_attempt += 1
                    _LOGGER.warning(
                        "500 UNEXPECTED_ERROR on %s %s (attempt %d/%d) — retrying in %.0fs",
                        method, url, five_hundred_attempt, policy.max_500_attempts,
                        policy.five_hundred_interval,
                    )
                    await asyncio.sleep(policy.five_hundred_interval)
                    continue
            else:
                if network_attempt < policy.max_retries:
                    network_attempt += 1
                    sleep = policy.backoff(network_attempt)
                    _LOGGER.warning(
                        "503 on %s %s (attempt %d/%d) — retrying in %.2fs",
                        method, url, network_attempt, policy.max_retries, sleep,
                    )
                    await asyncio.sleep(sleep)
                    continue
        return prepared


class SyncTransport:
    """Synchronous HTTP transport on top of ``httpx.Client``.

    Intended for internal use by ``AlfaBankClient`` and resources.
    """

    def __init__(
        self,
        config: ClientConfig,
        *,
        retry_policy: RetryPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.policy = retry_policy or RetryPolicy.from_config(config)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=config.timeout,
            verify=config.verify_ssl,
            proxy=config.proxy,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        response_model: type[T] | None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> T | None:
        url = _build_url(self.config, path)
        headers = _build_headers(self.config)
        prepared = _execute_sync(
            self._client, method, url, headers, json_body, params, self.config, self.policy,
        )
        if prepared.is_success():
            return parse_success(prepared, response_model)
        raise _extract_error(prepared)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SyncTransport":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class AsyncTransport:
    """Asynchronous HTTP transport on top of ``httpx.AsyncClient``."""

    def __init__(
        self,
        config: ClientConfig,
        *,
        retry_policy: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.policy = retry_policy or RetryPolicy.from_config(config)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout,
            verify=config.verify_ssl,
            proxy=config.proxy,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        response_model: type[T] | None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> T | None:
        url = _build_url(self.config, path)
        headers = _build_headers(self.config)
        prepared = await _execute_async(
            self._client, method, url, headers, json_body, params, self.config, self.policy,
        )
        if prepared.is_success():
            return parse_success(prepared, response_model)
        raise _extract_error(prepared)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AsyncTransport":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()


__all__ = [
    "AsyncTransport",
    "RetryPolicy",
    "SyncTransport",
    "_extract_error",
    "_map_error",
    "parse_success",
]
