"""Unit tests for the scoring resource."""
from __future__ import annotations

import httpx
import pytest
import respx

from alfabank_partner_api import (
    AlfaBankClient,
    ScoringDecision,
    ScoringRequest,
    ScoringTimeoutError,
)


def _submit_response(rid: str = "rq-1") -> dict:
    return {"requestId": rid}


@respx.mock
def test_submit_returns_request_id(client: AlfaBankClient) -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/scoring").mock(
        return_value=httpx.Response(200, json=_submit_response("abc"))
    )
    res = client.scoring.submit(ScoringRequest(inn="1234567890"))
    assert res.request_id == "abc"


@respx.mock
def test_get_result_accept_with_limits(
    client: AlfaBankClient, fixture_loader: object
) -> None:
    payload = fixture_loader("scoring_accept_with_limits.json")  # type: ignore[operator]
    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        return_value=httpx.Response(200, json=payload)
    )
    res = client.scoring.get_result("rq")
    assert res.decision == ScoringDecision.ACCEPT
    assert res.limits is not None
    assert res.limits[0].limit_max_sum.amount == 7_500_000


@respx.mock
def test_get_result_accept_without_limits(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        return_value=httpx.Response(200, json={
            "requestId": "rq", "decision": "ACCEPT", "limits": None,
        })
    )
    res = client.scoring.get_result("rq")
    assert res.decision == ScoringDecision.ACCEPT
    assert res.limits is None


@respx.mock
def test_get_result_decline(client: AlfaBankClient, fixture_loader: object) -> None:
    payload = fixture_loader("scoring_decline.json")  # type: ignore[operator]
    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        return_value=httpx.Response(200, json=payload)
    )
    res = client.scoring.get_result("rq")
    assert res.decision == ScoringDecision.DECLINE


@respx.mock
def test_wait_for_result_succeeds_after_pending(client: AlfaBankClient) -> None:
    sleeps: list[float] = []

    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        side_effect=[
            httpx.Response(200, json={"requestId": "rq", "decision": "PENDING"}),
            httpx.Response(200, json={"requestId": "rq", "decision": "PENDING"}),
            httpx.Response(200, json={"requestId": "rq", "decision": "ACCEPT"}),
        ]
    )
    res = client.scoring.wait_for_result(
        "rq", max_attempts=5, interval=0.0, sleep=sleeps.append,
    )
    assert res.decision == ScoringDecision.ACCEPT
    assert sleeps == [0.0, 0.0]


@respx.mock
def test_wait_for_result_timeout(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/scoring/rq").mock(
        return_value=httpx.Response(200, json={
            "requestId": "rq", "decision": "PENDING",
        }),
    )
    with pytest.raises(ScoringTimeoutError) as info:
        client.scoring.wait_for_result(
            "rq", max_attempts=3, interval=0.0, sleep=lambda _s: None,
        )
    assert info.value.attempts == 3
    assert info.value.request_id == "rq"
