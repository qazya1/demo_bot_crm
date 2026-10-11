"""Integration test: create a lead in the demo environment.

Runs only when ``ALFABANK_RUN_INTEGRATION=1`` is set in env.
"""
from __future__ import annotations

import os

import pytest

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

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("ALFABANK_RUN_INTEGRATION") != "1",
    reason="Set ALFABANK_RUN_INTEGRATION=1 to run integration tests",
)
def test_demo_create_lead_returns_id() -> None:
    cfg = ClientConfig(api_key=DEMO_API_KEY)
    req = LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="Demo OOO", inn="7707083893"),
        contact_info=[ContactInfo(phone_number="79991234567")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    with AlfaBankClient(config=cfg) as client:
        result = client.leads.create(req)
    assert result.id
