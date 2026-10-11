"""Models for the duplicate-check endpoint (section 4 of the API documentation)."""
from __future__ import annotations

from pydantic import Field

from .common import ApiError, _BaseAPIModel
from .leads import ProductItem


class _CheckOrganizationInfo(_BaseAPIModel):
    """Minimal organisation block accepted by ``POST /checks``."""

    inn: str = Field(..., pattern=r"^(\d{10}|\d{12})$")


class _CheckContactInfo(_BaseAPIModel):
    """Minimal contact block accepted by ``POST /checks``."""

    phone_number: str = Field(..., pattern=r"^7\d{10}$")


class DuplicateCheckRequest(_BaseAPIModel):
    """Payload for ``POST /checks``."""

    organization_info: _CheckOrganizationInfo
    contact_info: list[_CheckContactInfo] | None = Field(default=None)
    product_info: list[ProductItem] = Field(..., min_length=1)


class DuplicateCheckResult(_BaseAPIModel):
    """Result of a duplicate check.

    Attributes:
        is_duplicate: ``True`` when the API reported the lead as a duplicate
            (HTTP 403 ``UNACCEPTABLE_COMPANY``).
        error: The API error object when the API returned an error indicating
            duplicate, otherwise ``None``.
    """

    is_duplicate: bool = False
    error: ApiError | None = None
