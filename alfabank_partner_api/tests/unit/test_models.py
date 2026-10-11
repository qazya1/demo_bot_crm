"""Unit tests for pydantic models."""
from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError as PydanticValidationError

from alfabank_partner_api import (
    ContactInfo,
    LeadCreateRequest,
    LeadInfo,
    LeadSummary,
    OrganizationInfo,
    ProductCode,
    ProductItem,
    ProductState,
    RequestInfo,
    ReserveAccount,
    ScoringDecision,
    ScoringResult,
)


# --- INN / OGRN / KPP / phone / email -----------------------------------

@pytest.mark.parametrize("value", ["1234567890", "123456789012"])
def test_inn_valid(value: str) -> None:
    OrganizationInfo(organization_name="X", inn=value)


@pytest.mark.parametrize("value", ["1234", "abcdefghij", "12345678901", ""])
def test_inn_invalid(value: str) -> None:
    with pytest.raises(PydanticValidationError):
        OrganizationInfo(organization_name="X", inn=value)


@pytest.mark.parametrize("value", ["1234567890123", "123456789012345"])
def test_ogrn_valid(value: str) -> None:
    OrganizationInfo(organization_name="X", inn="1234567890", ogrn=value)


@pytest.mark.parametrize("value", ["12345678901234", "X234567890123"])
def test_ogrn_invalid(value: str) -> None:
    with pytest.raises(PydanticValidationError):
        OrganizationInfo(organization_name="X", inn="1234567890", ogrn=value)


def test_kpp_valid() -> None:
    OrganizationInfo(organization_name="X", inn="1234567890", kpp="123456789")


@pytest.mark.parametrize("value", ["12345", "1234567890"])
def test_kpp_invalid(value: str) -> None:
    with pytest.raises(PydanticValidationError):
        OrganizationInfo(organization_name="X", inn="1234567890", kpp=value)


@pytest.mark.parametrize("value", ["79991234567"])
def test_phone_valid(value: str) -> None:
    ContactInfo(phone_number=value)


@pytest.mark.parametrize("value", ["89991234567", "7999123456", "+79991234567"])
def test_phone_invalid(value: str) -> None:
    with pytest.raises(PydanticValidationError):
        ContactInfo(phone_number=value)


def test_extra_phone_numbers_validated() -> None:
    with pytest.raises(PydanticValidationError):
        ContactInfo(phone_number="79991234567", extra_phone_numbers=["bad"])
    ok = ContactInfo(phone_number="79991234567", extra_phone_numbers=["79992223344"])
    assert ok.extra_phone_numbers == ["79992223344"]


def test_email_validated() -> None:
    with pytest.raises(PydanticValidationError):
        ContactInfo(phone_number="79991234567", contact_email="not-an-email")


# --- camelCase serialisation --------------------------------------------

def test_serialisation_uses_camel_case() -> None:
    req = LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="ООО X", inn="1234567890"),
        contact_info=[ContactInfo(phone_number="79991234567")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    payload = req.to_api_dict()
    assert "organizationInfo" in payload
    assert "contactInfo" in payload
    assert "productInfo" in payload
    assert payload["organizationInfo"]["organizationName"] == "ООО X"
    assert payload["organizationInfo"]["inn"] == "1234567890"
    assert payload["productInfo"][0]["productCode"] == "LP_RKO"


def test_deserialisation_from_camel_case() -> None:
    raw = {
        "uuid": "lead-1",
        "state": "IN_PROGRESS",
        "status": "ok",
        "detailedStatus": "wait",
        "productInfo": [
            {"productCode": "LP_RKO", "state": "NEW"},
        ],
    }
    summary = LeadSummary.model_validate(raw)
    assert summary.uuid == "lead-1"
    assert summary.detailed_status == "wait"
    assert summary.product_info[0].product_code == ProductCode.LP_RKO
    assert summary.product_info[0].state == ProductState.NEW


def test_lead_info_from_fixture(fixture_loader: object) -> None:
    raw = fixture_loader("lead_info_with_deal_amount.json")  # type: ignore[operator]
    info = LeadInfo.model_validate(raw)
    assert info.product_info[0].product_params is not None
    assert info.product_info[0].product_params.deal_fin_amount == 5000000


# --- Reserve account conditional validation -----------------------------

def _base_org() -> OrganizationInfo:
    return OrganizationInfo(
        organization_name="ООО X",
        inn="1234567890",
        ogrn="1234567890123",
        kpp="123456789",
    )


def test_reserve_account_requires_all_extras() -> None:
    with pytest.raises(PydanticValidationError):
        LeadCreateRequest(
            organization_info=OrganizationInfo(organization_name="X", inn="1234567890"),
            contact_info=[ContactInfo(phone_number="79991234567")],
            request_info=RequestInfo(city_code="MSK"),
            product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
            reserve_account=ReserveAccount(
                branch_id="B1",
                address="Москва, 1",
                email="a@b.com",
            ),
        )


def test_reserve_account_ok_when_all_present() -> None:
    LeadCreateRequest(
        organization_info=_base_org(),
        contact_info=[ContactInfo(phone_number="79991234567", contact_email="a@b.com")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
        reserve_account=ReserveAccount(
            branch_id="B1",
            address="Москва, 1",
            email="a@b.com",
        ),
    )


# --- Lenient enum parsing -----------------------------------------------

def test_unknown_state_is_kept_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="alfabank_partner_api"):
        result = LeadSummary.model_validate({
            "uuid": "x",
            "state": "TOTALLY_NEW_STATE",
            "productInfo": [],
        })
    assert result.state == "TOTALLY_NEW_STATE"
    assert any("TOTALLY_NEW_STATE" in r.message for r in caplog.records)


def test_unknown_decision_is_kept_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="alfabank_partner_api"):
        result = ScoringResult.model_validate({
            "requestId": "x",
            "decision": "REVIEW",
        })
    assert result.decision == "REVIEW"
    assert any("REVIEW" in r.message for r in caplog.records)


def test_known_decision_is_enum() -> None:
    result = ScoringResult.model_validate({
        "requestId": "x",
        "decision": "ACCEPT",
    })
    assert result.decision == ScoringDecision.ACCEPT
