"""Integration tests: duplicate check on the demo environment.

Demo behaviour from section 1.2 of the documentation:
    * INN starting with ``1`` → ``UNACCEPTABLE_COMPANY``
    * Other INNs → 200 OK (not a duplicate)
"""
from __future__ import annotations

import os

import pytest

from alfabank_partner_api import (
    AlfaBankClient,
    ClientConfig,
    DEMO_API_KEY,
    DuplicateCheckRequest,
    ProductCode,
    ProductItem,
)
from alfabank_partner_api.models.checks import _CheckOrganizationInfo

pytestmark = pytest.mark.integration

_INTEGRATION_ENABLED = os.environ.get("ALFABANK_RUN_INTEGRATION") == "1"


@pytest.mark.skipif(not _INTEGRATION_ENABLED, reason="Integration tests disabled")
def test_demo_check_duplicate_inn_starting_with_1() -> None:
    cfg = ClientConfig(api_key=DEMO_API_KEY)
    req = DuplicateCheckRequest(
        organization_info=_CheckOrganizationInfo(inn="1234567890"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    with AlfaBankClient(config=cfg) as client:
        result = client.checks.check_duplicate(req)
    assert result.is_duplicate is True


@pytest.mark.skipif(not _INTEGRATION_ENABLED, reason="Integration tests disabled")
def test_demo_check_duplicate_normal_inn() -> None:
    cfg = ClientConfig(api_key=DEMO_API_KEY)
    req = DuplicateCheckRequest(
        organization_info=_CheckOrganizationInfo(inn="7707083893"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    with AlfaBankClient(config=cfg) as client:
        result = client.checks.check_duplicate(req)
    assert result.is_duplicate is False
