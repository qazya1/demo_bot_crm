"""Unit tests for logging masking utilities and integration."""
from __future__ import annotations

import logging
import re

import httpx
import pytest
import respx

from alfabank_partner_api import (
    AlfaBankClient,
    ClientConfig,
    ContactInfo,
    DEMO_API_KEY,
    LeadCreateRequest,
    OrganizationInfo,
    ProductCode,
    ProductItem,
    RequestInfo,
)
from alfabank_partner_api.logging import mask_api_key, mask_pii, safe_headers


def test_mask_api_key_basic() -> None:
    assert mask_api_key("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") == "aaaa***aaaa"
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "***"


def test_safe_headers_masks_api_key() -> None:
    headers = {"API-key": DEMO_API_KEY, "User-Agent": "ua/1.0"}
    safe = safe_headers(headers)
    assert safe["API-key"] != DEMO_API_KEY
    assert "***" in safe["API-key"]
    assert safe["User-Agent"] == "ua/1.0"


def test_mask_pii_dict() -> None:
    raw = {
        "organizationInfo": {"inn": "1234567890", "organizationName": "ООО X"},
        "contactInfo": [
            {"phoneNumber": "79991234567", "contactEmail": "user@example.com"},
        ],
    }
    masked = mask_pii(raw)
    assert masked["organizationInfo"]["inn"] != "1234567890"
    assert masked["organizationInfo"]["organizationName"] == "ООО X"
    assert masked["contactInfo"][0]["phoneNumber"] != "79991234567"
    assert masked["contactInfo"][0]["contactEmail"] != "user@example.com"


@respx.mock
def test_api_key_never_logged_in_full(caplog: pytest.LogCaptureFixture) -> None:
    cfg = ClientConfig(
        api_key=DEMO_API_KEY,
        timeout=5.0,
        max_retries=0,
        retry_backoff_base=0.0,
    )
    respx.post("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(201, json={"id": "demo-1"})
    )
    req = LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="X", inn="1234567890"),
        contact_info=[
            ContactInfo(phone_number="79991234567", contact_email="user@example.com")
        ],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    with caplog.at_level(logging.DEBUG, logger="alfabank_partner_api"):
        with AlfaBankClient(config=cfg) as c:
            c.leads.create(req)
    full_text = "\n".join(r.getMessage() for r in caplog.records)
    assert DEMO_API_KEY not in full_text


@respx.mock
def test_pii_masked_when_flag_true(caplog: pytest.LogCaptureFixture) -> None:
    cfg = ClientConfig(
        api_key=DEMO_API_KEY,
        timeout=5.0,
        max_retries=0,
        retry_backoff_base=0.0,
        mask_pii_in_logs=True,
    )
    respx.post("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(201, json={"id": "demo-1"})
    )
    req = LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="X", inn="9876543210"),
        contact_info=[ContactInfo(phone_number="79997776655", contact_email="zzz@zz.com")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    with caplog.at_level(logging.DEBUG, logger="alfabank_partner_api"):
        with AlfaBankClient(config=cfg) as c:
            c.leads.create(req)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "9876543210" not in text
    assert "79997776655" not in text
    assert "zzz@zz.com" not in text


@respx.mock
def test_pii_visible_when_flag_false(caplog: pytest.LogCaptureFixture) -> None:
    cfg = ClientConfig(
        api_key=DEMO_API_KEY,
        timeout=5.0,
        max_retries=0,
        retry_backoff_base=0.0,
        mask_pii_in_logs=False,
    )
    respx.post("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(201, json={"id": "demo-1"})
    )
    req = LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="X", inn="9876543210"),
        contact_info=[ContactInfo(phone_number="79997776655")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    with caplog.at_level(logging.DEBUG, logger="alfabank_partner_api"):
        with AlfaBankClient(config=cfg) as c:
            c.leads.create(req)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "9876543210" in text or "79997776655" in text


def test_module_does_not_modify_root_logger() -> None:
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    # Importing alfabank_partner_api in a fresh interpreter would be cleaner,
    # but reimporting here is enough to validate idempotency.
    import importlib

    import alfabank_partner_api

    importlib.reload(alfabank_partner_api)
    assert root.handlers == handlers_before
    assert root.level == level_before
    # Module logger has at least the NullHandler the module installs
    pkg_logger = logging.getLogger("alfabank_partner_api")
    assert any(isinstance(h, logging.NullHandler) for h in pkg_logger.handlers)


def test_mask_api_key_does_not_match_partial_in_logs() -> None:
    masked = mask_api_key(DEMO_API_KEY)
    assert not re.fullmatch(DEMO_API_KEY, masked)
