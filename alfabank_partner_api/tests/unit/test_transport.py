"""Unit tests for transport layer: headers, retries, error mapping."""
from __future__ import annotations

import httpx
import pytest
import respx

from alfabank_partner_api import (
    AlfaBankAPIError,
    AlfaBankClient,
    AlfaBankNetworkError,
    AuthenticationError,
    AuthorizationError,
    BlockedPartnerError,
    ChecksLimitError,
    ClientError,
    DuplicateRequestError,
    LeadsLimitError,
    NewCompanyError,
    RateLimitError,
    ServerError,
    ServiceUnavailableError,
    UnacceptableCompanyError,
    ValidationError,
)


def _err(status: int, code: str, title: str = "t", detail: str = "d") -> dict:
    return {"errors": [{"status": status, "code": code, "title": title, "detail": detail}]}


@respx.mock
def test_request_sets_required_headers(client: AlfaBankClient) -> None:
    route = respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
        return_value=httpx.Response(200, json={"uuid": "x", "state": "SENT", "productInfo": []}),
    )
    client.leads.get("x")
    request = route.calls.last.request
    assert request.headers["API-key"] == client.config.api_key
    assert request.headers["Content-Type"].startswith("application/json")
    assert request.headers["User-Agent"].startswith("alfabank-partner-api-python/")


@respx.mock
def test_success_200_parsed(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/leads/abc").mock(
        return_value=httpx.Response(
            200, json={"uuid": "abc", "state": "IN_PROGRESS", "productInfo": []}
        ),
    )
    info = client.leads.get("abc")
    assert info.uuid == "abc"


@pytest.mark.parametrize(
    "status,code,exc",
    [
        (401, "NO_REQUIRED_HEADER", AuthenticationError),
        (401, "WRONG_PARAMETERS", AuthenticationError),
        (403, "UNACCEPTABLE_COMPANY", UnacceptableCompanyError),
        (403, "BLOCKED_PARTNER", BlockedPartnerError),
        (403, "LEADS_LIMIT_ERROR", LeadsLimitError),
        (403, "CHECKS_LIMIT_ERROR", ChecksLimitError),
        (403, "NEW_COMPANY_ERROR", NewCompanyError),
        (403, "SOMETHING_ELSE", AuthorizationError),
        (400, "validationError", ValidationError),
        (400, "CLIENT_ERROR", ClientError),
        (429, "RATE_LIMIT_EXCEEDED", RateLimitError),
        (500, "Repeated request error", DuplicateRequestError),
        (500, "UNEXPECTED_ERROR", ServerError),
        (503, "SERVICE_UNAVAILABLE", ServiceUnavailableError),
    ],
)
@respx.mock
def test_error_mapping(
    client: AlfaBankClient, status: int, code: str, exc: type[Exception]
) -> None:
    # 503 / 500-UNEXPECTED would be retried — return persistently to exhaust retries.
    respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
        return_value=httpx.Response(status, json=_err(status, code))
    )
    with pytest.raises(exc) as info:
        client.leads.get("x")
    assert info.value.status_code == status
    assert info.value.error_code == code


@respx.mock
def test_retry_on_503_then_success(client: AlfaBankClient) -> None:
    route = respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
        side_effect=[
            httpx.Response(503, json=_err(503, "SERVICE_UNAVAILABLE")),
            httpx.Response(200, json={"uuid": "x", "state": "SENT", "productInfo": []}),
        ],
    )
    info = client.leads.get("x")
    assert info.uuid == "x"
    assert route.call_count == 2


@respx.mock
def test_no_retry_on_400_401_403_429(client: AlfaBankClient) -> None:
    for status, code, exc in [
        (400, "validationError", ValidationError),
        (401, "NO_REQUIRED_HEADER", AuthenticationError),
        (403, "BLOCKED_PARTNER", BlockedPartnerError),
        (429, "RATE_LIMIT_EXCEEDED", RateLimitError),
    ]:
        respx.reset()
        route = respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
            return_value=httpx.Response(status, json=_err(status, code))
        )
        with pytest.raises(exc):
            client.leads.get("x")
        assert route.call_count == 1, f"unexpected retry on {status} {code}"


@respx.mock
def test_no_retry_on_500_repeated_request(client: AlfaBankClient) -> None:
    route = respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
        return_value=httpx.Response(500, json=_err(500, "Repeated request error"))
    )
    with pytest.raises(DuplicateRequestError):
        client.leads.get("x")
    assert route.call_count == 1


@respx.mock
def test_invalid_json_body_raises_api_error(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
        return_value=httpx.Response(200, content=b"not-json"),
    )
    with pytest.raises(AlfaBankAPIError) as info:
        client.leads.get("x")
    assert info.value.error_code == "INVALID_JSON"


@respx.mock
def test_network_error_after_retries(client: AlfaBankClient) -> None:
    respx.get("https://partner.alfabank.ru/public-api/v2/leads/x").mock(
        side_effect=httpx.ConnectError("boom"),
    )
    with pytest.raises(AlfaBankNetworkError):
        client.leads.get("x")
