"""Async coverage of leads / scoring / dictionaries."""
from __future__ import annotations

import httpx
import pytest
import respx

from alfabank_partner_api import (
    AlfaBankAsyncClient,
    ClientConfig,
    DEMO_API_KEY,
    DictionaryCache,
    ProductCode,
    ScoringDecision,
)


def _config() -> ClientConfig:
    return ClientConfig(api_key=DEMO_API_KEY, max_retries=0, retry_backoff_base=0.0)


@pytest.mark.asyncio
@respx.mock
async def test_async_leads_get() -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
        return_value=httpx.Response(200, json={
            "uuid": "x", "state": "SENT", "productInfo": [
                {"productCode": "LP_RKO", "state": "NEW"}
            ],
        })
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        info = await client.leads.get("x")
    assert info.uuid == "x"
    assert info.product_info[0].product_code == ProductCode.LP_RKO


@pytest.mark.asyncio
@respx.mock
async def test_async_leads_list() -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(200, json=[
            {"uuid": "1", "state": "SENT", "productInfo": []},
            {"uuid": "2", "state": "CLOSED", "productInfo": []},
        ])
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        items = await client.leads.list(page=1, per_page=10)
    assert [x.uuid for x in items] == ["1", "2"]


@pytest.mark.asyncio
@respx.mock
async def test_async_dictionaries_with_cache() -> None:
    cache = DictionaryCache(ttl=60.0)
    route = respx.get("https://partner.alfabank.ru/public-api/v2/dictionaries").mock(
        return_value=httpx.Response(200, json=[{"code": "MSK", "name": "Москва"}])
    )
    async with AlfaBankAsyncClient(config=_config(), dictionary_cache=cache) as client:
        first = await client.dictionaries.cities()
        second = await client.dictionaries.cities()
    assert first == second
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_async_dictionaries_regions_branches() -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/dictionaries").mock(
        return_value=httpx.Response(200, json=[{"code": "77", "name": "MO"}])
    )
    respx.get("https://partner.alfabank.ru/public-api/v2/branches/abc").mock(
        return_value=httpx.Response(200, json=[{"branchId": "B1", "name": "ОПЕРУ"}])
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        regions = await client.dictionaries.regions()
        branches = await client.dictionaries.branches("abc")
    assert regions[0].code == "77"
    assert branches[0].branch_id == "B1"


@pytest.mark.asyncio
@respx.mock
async def test_async_wait_for_result_success() -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        side_effect=[
            httpx.Response(200, json={"requestId": "rq", "decision": "PENDING"}),
            httpx.Response(200, json={"requestId": "rq", "decision": "ACCEPT"}),
        ]
    )
    async with AlfaBankAsyncClient(config=_config()) as client:
        res = await client.scoring.wait_for_result("rq", max_attempts=3, interval=0.0)
    assert res.decision == ScoringDecision.ACCEPT
