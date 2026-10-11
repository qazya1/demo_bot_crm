"""Models for scoring (section 5 of the API documentation)."""
from __future__ import annotations

import logging
from typing import Any

from pydantic import Field, field_validator

from ..enums import ScoringDecision
from .common import _BaseAPIModel

_logger = logging.getLogger("alfabank_partner_api")


class ScoringRequest(_BaseAPIModel):
    """Payload for ``POST /scoring``."""

    inn: str = Field(..., pattern=r"^(\d{10}|\d{12})$")
    okved: str | None = Field(default=None)
    phone: str | None = Field(default=None, pattern=r"^7\d{10}$")


class ScoringSubmitResponse(_BaseAPIModel):
    """Response of ``POST /scoring``."""

    request_id: str = Field(..., min_length=1)


class LimitMaxSum(_BaseAPIModel):
    """Maximum sum block."""

    amount: int = Field(..., ge=0)


class Limit(_BaseAPIModel):
    """A single approved credit limit."""

    months_number: int = Field(..., ge=0)
    prod_code: str
    loan_product: str
    product_description: str | None = Field(default=None)
    limit_end_date: str | None = Field(default=None)
    interest_rate: float
    limit_max_sum: LimitMaxSum
    product: str | None = Field(default=None)
    credit_rate: float | None = Field(default=None)


class ScoringResult(_BaseAPIModel):
    """Final result of a scoring request."""

    request_id: str
    decision: ScoringDecision | str
    limits: list[Limit] | None = Field(default=None)
    company_credit_rating: str | None = Field(default=None)

    @field_validator("decision", mode="before")
    @classmethod
    def _decision_lenient(cls, value: Any) -> Any:
        if value is None or isinstance(value, ScoringDecision):
            return value
        try:
            return ScoringDecision(value)
        except ValueError:
            _logger.warning(
                "Unknown ScoringDecision value: %r — keeping as raw string", value
            )
            return value
