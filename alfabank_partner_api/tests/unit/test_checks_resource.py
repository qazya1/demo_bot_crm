"""Unit tests for the checks resource."""
from __future__ import annotations

import httpx
import pytest
import respx

from alfabank_partner_api import (
    AlfaBankClient,
    BlockedPartnerError,
    DuplicateCheckRequest,
    ProductCode,
    ProductItem,
)
from alfabank_partner_api.models.checks import (
    _CheckContactInfo,
    _CheckOrganizationInfo,
)


def _request() -> DuplicateCheckRequest:
    return DuplicateCheckRequest(
        organization_info=_CheckOrganizationInfo(inn="1234567890"),
        contact_info=[_CheckContactInfo(phone_number="79991234567")],
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )


@respx.mock
def test_check_duplicate_200_means_not_duplicate(client: AlfaBankClient) -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/checks").mock(
        return_value=httpx.Response(200, content=b""),
    )
    result = client.checks.check_duplicate(_request())
    assert result.is_duplicate is False
    assert result.error is None


@respx.mock
def test_check_duplicate_unacceptable_company_returns_result(
    client: AlfaBankClient,
) -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/checks").mock(
        return_value=httpx.Response(403, json={
            "errors": [{
                "status": 403, "code": "UNACCEPTABLE_COMPANY",
                "title": "Unacceptable company", "detail": "duplicate",
            }],
        }),
    )
    result = client.checks.check_duplicate(_request())
    assert result.is_duplicate is True
    assert result.error is not None
    assert result.error.code == "UNACCEPTABLE_COMPANY"


@respx.mock
def test_check_duplicate_blocked_partner_raises(client: AlfaBankClient) -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/checks").mock(
        return_value=httpx.Response(403, json={
            "errors": [{
                "status": 403, "code": "BLOCKED_PARTNER",
                "title": "Blocked", "detail": "blocked",
            }],
        }),
    )
    with pytest.raises(BlockedPartnerError):
        client.checks.check_duplicate(_request())
