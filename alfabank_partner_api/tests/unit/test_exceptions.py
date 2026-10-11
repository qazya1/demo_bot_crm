"""Unit tests for the exception hierarchy."""
from __future__ import annotations

from alfabank_partner_api import (
    AlfaBankAPIError,
    UnacceptableCompanyError,
)


def test_str_format() -> None:
    exc = UnacceptableCompanyError(
        status_code=403,
        error_code="UNACCEPTABLE_COMPANY",
        title="Unacceptable company",
        detail="exists",
    )
    assert str(exc) == "[403 UNACCEPTABLE_COMPANY] Unacceptable company: exists"


def test_str_without_detail() -> None:
    exc = AlfaBankAPIError(
        status_code=400, error_code="X", title="oops", detail=None,
    )
    assert str(exc) == "[400 X] oops"


def test_attributes_accessible() -> None:
    exc = AlfaBankAPIError(
        status_code=500, error_code="X", title="t", detail="d", raw_response={"k": "v"},
    )
    assert exc.status_code == 500
    assert exc.error_code == "X"
    assert exc.title == "t"
    assert exc.detail == "d"
    assert exc.raw_response == {"k": "v"}


def test_to_dict_serialisable() -> None:
    import json

    exc = AlfaBankAPIError(
        status_code=500, error_code="X", title="t", detail="d", raw_response={"k": "v"},
    )
    payload = exc.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["status_code"] == 500
