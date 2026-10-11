"""Models related to leads (sections 2, 3.1, 3.2 of the API documentation)."""
from __future__ import annotations

import logging
from typing import Any

from pydantic import EmailStr, Field, field_validator, model_validator

from ..enums import LeadState, ProductCode, ProductState
from .common import _BaseAPIModel

_logger = logging.getLogger("alfabank_partner_api")

_INN_RE = r"^(\d{10}|\d{12})$"
_OGRN_RE = r"^(\d{13}|\d{15})$"
_KPP_RE = r"^\d{9}$"
_PHONE_RE = r"^7\d{10}$"


class OrganizationInfo(_BaseAPIModel):
    """Organisation block of a lead."""

    organization_name: str = Field(..., min_length=1)
    inn: str = Field(..., pattern=_INN_RE)
    ogrn: str | None = Field(default=None, pattern=_OGRN_RE)
    kpp: str | None = Field(default=None, pattern=_KPP_RE)


class ContactInfo(_BaseAPIModel):
    """Contact-person block."""

    full_name: str | None = Field(default=None)
    phone_number: str = Field(..., pattern=_PHONE_RE)
    contact_email: EmailStr | None = Field(default=None)
    contractors_position: str | None = Field(default=None)
    extra_phone_numbers: list[str] = Field(default_factory=list)

    @field_validator("extra_phone_numbers")
    @classmethod
    def _validate_extra_phones(cls, value: list[str]) -> list[str]:
        import re

        rx = re.compile(_PHONE_RE)
        for phone in value:
            if not rx.match(phone):
                raise ValueError(f"extra phone number does not match {_PHONE_RE}: {phone!r}")
        return value


class RequestInfo(_BaseAPIModel):
    """Auxiliary request metadata."""

    adv_code: str | None = Field(default=None)
    platform_id: str | None = Field(default=None)
    comment: str | None = Field(default=None)
    city_code: str = Field(..., min_length=1)


class ProductItem(_BaseAPIModel):
    """A single product code in a request."""

    product_code: ProductCode


class ReserveAccount(_BaseAPIModel):
    """Reserve-account block (optional addition to a lead)."""

    branch_id: str = Field(..., min_length=1)
    address: str = Field(..., min_length=1)
    email: EmailStr


def _parse_lenient_enum(value: Any, enum_cls: type, field: str) -> Any:
    """Return ``value`` parsed as ``enum_cls`` or the raw string with a warning."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        _logger.warning(
            "Unknown value for %s: %r — keeping as raw string", field, value
        )
        return value


class LeadCreateRequest(_BaseAPIModel):
    """Payload for ``POST /leads``."""

    organization_info: OrganizationInfo
    contact_info: list[ContactInfo] = Field(..., min_length=1)
    request_info: RequestInfo
    product_info: list[ProductItem] = Field(..., min_length=1)
    reserve_account: ReserveAccount | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_reserve_account_requirements(self) -> "LeadCreateRequest":
        if self.reserve_account is None:
            return self
        org = self.organization_info
        missing: list[str] = []
        if not org.ogrn:
            missing.append("organization_info.ogrn")
        if not org.kpp:
            missing.append("organization_info.kpp")
        if not any(c.contact_email for c in self.contact_info):
            missing.append("contact_info[*].contact_email")
        if missing:
            raise ValueError(
                "reserve_account requires the following fields to be present: "
                + ", ".join(missing)
            )
        return self


class LeadCreateResponse(_BaseAPIModel):
    """Response of ``POST /leads``."""

    id: str = Field(..., min_length=1)


class ProductParams(_BaseAPIModel):
    """Per-product details inside a lead status."""

    deal_fin_amount: int | None = Field(default=None, ge=0)


class ProductStatus(_BaseAPIModel):
    """Status of a single product inside a lead."""

    product_code: ProductCode | str
    state: ProductState | str
    status: str | None = Field(default=None)
    product_params: ProductParams | None = Field(default=None)

    @field_validator("state", mode="before")
    @classmethod
    def _state_lenient(cls, value: Any) -> Any:
        return _parse_lenient_enum(value, ProductState, "ProductStatus.state")

    @field_validator("product_code", mode="before")
    @classmethod
    def _product_code_lenient(cls, value: Any) -> Any:
        return _parse_lenient_enum(value, ProductCode, "ProductStatus.product_code")


class LeadSummary(_BaseAPIModel):
    """Summary item from ``GET /leads``."""

    uuid: str
    state: LeadState | str
    status: str | None = Field(default=None)
    detailed_status: str | None = Field(default=None)
    product_info: list[ProductStatus] = Field(default_factory=list)

    @field_validator("state", mode="before")
    @classmethod
    def _state_lenient(cls, value: Any) -> Any:
        return _parse_lenient_enum(value, LeadState, "LeadSummary.state")


class LeadInfo(LeadSummary):
    """Full lead info from ``GET /leads/{id}``.

    Currently identical to ``LeadSummary``; kept as a separate class for
    forward compatibility (the API may add fields here).
    """
