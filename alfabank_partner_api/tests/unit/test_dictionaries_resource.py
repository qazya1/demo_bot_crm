"""Unit tests for the dictionaries resource and its cache."""
from __future__ import annotations

import httpx
import respx

from alfabank_partner_api import (
    AlfaBankClient,
    ClientConfig,
    DictionaryCache,
    DEMO_API_KEY,
)


@respx.mock
def test_cities_parsed(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/dictionaries").mock(
        return_value=httpx.Response(200, json=[
            {"code": "MSK", "name": "Москва", "fiasId": "abc"},
        ])
    )
    cities = client.dictionaries.cities()
    assert len(cities) == 1
    assert cities[0].code == "MSK"
    assert cities[0].fias_id == "abc"


@respx.mock
def test_regions_parsed(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/dictionaries").mock(
        return_value=httpx.Response(200, json=[
            {"code": "77", "name": "Московская область"},
        ])
    )
    regions = client.dictionaries.regions()
    assert regions[0].code == "77"


@respx.mock
def test_branches_parsed(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/branches/abc").mock(
        return_value=httpx.Response(200, json=[
            {"branchId": "B1", "name": "ОПЕРУ", "address": "Москва"},
        ])
    )
    branches = client.dictionaries.branches("abc")
    assert branches[0].branch_id == "B1"


@respx.mock
def test_cache_avoids_second_http_call() -> None:
    cache = DictionaryCache(ttl=60.0)
    cfg = ClientConfig(api_key=DEMO_API_KEY, max_retries=0, retry_backoff_base=0.1)
    route = respx.get("https://partner.alfabank.ru/public-api/v2/dictionaries").mock(
        return_value=httpx.Response(200, json=[{"code": "MSK", "name": "Москва"}])
    )
    with AlfaBankClient(config=cfg, dictionary_cache=cache) as c:
        first = c.dictionaries.cities()
        second = c.dictionaries.cities()
    assert first == second
    assert route.call_count == 1


@respx.mock
def test_cache_disabled_makes_each_call() -> None:
    cfg = ClientConfig(api_key=DEMO_API_KEY, max_retries=0, retry_backoff_base=0.1)
    route = respx.get("https://partner.alfabank.ru/public-api/v2/dictionaries").mock(
        return_value=httpx.Response(200, json=[{"code": "MSK", "name": "Москва"}])
    )
    with AlfaBankClient(config=cfg) as c:
        c.dictionaries.cities()
        c.dictionaries.cities()
    assert route.call_count == 2


def test_cache_ttl_none_is_passthrough() -> None:
    cache = DictionaryCache(ttl=None)
    cache.set("k", [1, 2, 3])
    assert cache.get("k") is None


def test_cache_clear() -> None:
    cache = DictionaryCache(ttl=60.0)
    cache.set("k", [1])
    cache.clear()
    assert cache.get("k") is None
