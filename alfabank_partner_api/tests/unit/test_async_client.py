"""Async-client unit tests."""
from __future__ import annotations

import httpx
import pytest
import respx

from alfabank_partner_api import (
    AlfaBankAsyncClient,
    ClientConfig,
    ContactInfo,
    DEMO_API_KEY,
    LeadCreateRequest,
    OrganizationInfo,
    ProductCode,
    ProductItem,
    RequestInfo,
    ScoringDecision,
    ScoringRequest,
    ScoringTimeoutError,
)


def _config() -> ClientConfig:
    return ClientConfig(
        api_key=DEMO_API_KEY,
        max_retries=0,
        retry_backoff_base=0.1,
    )


@pytest.mark.asyncio
@respx.mock
async def test_async_create_lead() -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(201, json={"id": "async-1"})
    )
    req = LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="X", inn="1234567890"),
        contact_info=[ContactInfo(phone_number="79991234567")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        res = await client.leads.create(req)
    assert res.id == "async-1"


@pytest.mark.asyncio
@respx.mock
async def test_async_check_duplicate_returns_result() -> None:
    from alfabank_partner_api.models.checks import (
        DuplicateCheckRequest,
        _CheckOrganizationInfo,
    )

    respx.post("https://partner.alfabank.ru/public-api/v2/checks").mock(
        return_value=httpx.Response(403, json={
            "errors": [{
                "status": 403, "code": "UNACCEPTABLE_COMPANY",
                "title": "dup", "detail": "x",
            }],
        }),
    )
    req = DuplicateCheckRequest(
        organization_info=_CheckOrganizationInfo(inn="1234567890"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        res = await client.checks.check_duplicate(req)
    assert res.is_duplicate is True


@pytest.mark.asyncio
@respx.mock
async def test_async_wait_for_result_timeout() -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/scoring").mock(
        return_value=httpx.Response(200, json={"requestId": "rq"})
    )
    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        return_value=httpx.Response(200, json={"requestId": "rq", "decision": "PENDING"}),
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        await client.scoring.submit(ScoringRequest(inn="1234567890"))
        with pytest.raises(ScoringTimeoutError):
            await client.scoring.wait_for_result("rq", max_attempts=2, interval=0.0)


@pytest.mark.asyncio
@respx.mock
async def test_async_get_result_decline() -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        return_value=httpx.Response(200, json={
            "requestId": "rq", "decision": "DECLINE",
        })
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        res = await client.scoring.get_result("rq")
    assert res.decision == ScoringDecision.DECLINE
