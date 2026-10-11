"""Exception hierarchy for the Alfa-Bank partner API SDK."""
from __future__ import annotations

from typing import Any


class AlfaBankError(Exception):
    """Base exception for any error raised by the SDK."""


class AlfaBankConfigurationError(AlfaBankError):
    """Raised when SDK configuration is invalid (missing api_key, etc.)."""


class AlfaBankNetworkError(AlfaBankError):
    """Raised on connection / timeout / DNS errors after retries are exhausted."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.__cause__ = cause


class AlfaBankAPIError(AlfaBankError):
    """Base class for any HTTP error returned by the API (status >= 400)."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        title: str,
        detail: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.title = title
        self.detail = detail
        self.raw_response: dict[str, Any] = raw_response or {}
        super().__init__(self._format())

    def _format(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"[{self.status_code} {self.error_code}] {self.title}{suffix}"

    def __str__(self) -> str:
        return self._format()

    def to_dict(self) -> dict[str, Any]:
        """Serialise the exception to a dict (for logging / tracking)."""
        return {
            "status_code": self.status_code,
            "error_code": self.error_code,
            "title": self.title,
            "detail": self.detail,
            "raw_response": self.raw_response,
        }


class AuthenticationError(AlfaBankAPIError):
    """HTTP 401 — missing or wrong credentials."""


class AuthorizationError(AlfaBankAPIError):
    """HTTP 403 — generic authorization error."""


class UnacceptableCompanyError(AuthorizationError):
    """HTTP 403 UNACCEPTABLE_COMPANY — duplicate / unacceptable company."""


class BlockedPartnerError(AuthorizationError):
    """HTTP 403 BLOCKED_PARTNER — partner is blocked."""


class LeadsLimitError(AuthorizationError):
    """HTTP 403 LEADS_LIMIT_ERROR — daily leads limit exceeded."""


class ChecksLimitError(AuthorizationError):
    """HTTP 403 CHECKS_LIMIT_ERROR — daily checks limit exceeded."""


class NewCompanyError(AuthorizationError):
    """HTTP 403 NEW_COMPANY_ERROR — company too new for this product."""


class ValidationError(AlfaBankAPIError):
    """HTTP 400 validationError — request payload validation failed on the server side."""


class ClientError(AlfaBankAPIError):
    """HTTP 400 CLIENT_ERROR — business logic error (e.g. no scoring for a loan product)."""


class RateLimitError(AlfaBankAPIError):
    """HTTP 429 RATE_LIMIT_EXCEEDED — too many requests."""


class DuplicateRequestError(AlfaBankAPIError):
    """HTTP 500 with code "Repeated request error" — duplicate POST within 15 minutes."""


class ServerError(AlfaBankAPIError):
    """HTTP 500 UNEXPECTED_ERROR — generic upstream error."""


class ServiceUnavailableError(AlfaBankAPIError):
    """HTTP 503 SERVICE_UNAVAILABLE — temporary outage."""


class ScoringTimeoutError(AlfaBankError):
    """Raised by ``scoring.wait_for_result`` when polling times out."""

    def __init__(self, request_id: str, attempts: int) -> None:
        self.request_id = request_id
        self.attempts = attempts
        super().__init__(
            f"Scoring request {request_id} did not finish after {attempts} polling attempts"
        )
