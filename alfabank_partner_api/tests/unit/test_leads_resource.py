"""Unit tests for the leads resource."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from alfabank_partner_api import (
    AlfaBankClient,
    ClientError,
    ContactInfo,
    LeadCreateRequest,
    OrganizationInfo,
    ProductCode,
    ProductItem,
    RequestInfo,
    ReserveAccount,
)


def _request_min(product: ProductCode = ProductCode.LP_RKO) -> LeadCreateRequest:
    return LeadCreateRequest(
        organization_info=OrganizationInfo(organization_name="X", inn="1234567890"),
        contact_info=[ContactInfo(phone_number="79991234567")],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=product)],
    )


@respx.mock
def test_create_minimal(client: AlfaBankClient) -> None:
    route = respx.post("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(201, json={"id": "demo-1"})
    )
    res = client.leads.create(_request_min())
    assert res.id == "demo-1"
    body = route.calls.last.request.read().decode()
    assert "organizationInfo" in body
    assert "contactInfo" in body
    assert "productInfo" in body


@respx.mock
def test_create_with_reserve_account(client: AlfaBankClient) -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(201, json={"id": "with-reserve"})
    )
    req = LeadCreateRequest(
        organization_info=OrganizationInfo(
            organization_name="X",
            inn="1234567890",
            ogrn="1234567890123",
            kpp="123456789",
        ),
        contact_info=[
            ContactInfo(phone_number="79991234567", contact_email="a@b.com")
        ],
        request_info=RequestInfo(city_code="MSK"),
        product_info=[ProductItem(product_code=ProductCode.LP_RKO)],
        reserve_account=ReserveAccount(
            branch_id="B1", address="Москва, 1", email="a@b.com",
        ),
    )
    res = client.leads.create(req)
    assert res.id == "with-reserve"


@respx.mock
def test_create_loan_without_scoring_raises_client_error(client: AlfaBankClient) -> None:
    respx.post("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(
            400,
            json={"errors": [{
                "status": 400, "code": "CLIENT_ERROR",
                "title": "scoring required",
                "detail": "loan products need a prior scoring",
            }]},
        )
    )
    with pytest.raises(ClientError):
        client.leads.create(_request_min(ProductCode.LP_LOAN_BUS))


@respx.mock
def test_list_pagination_and_dates(client: AlfaBankClient) -> None:
    route = respx.get("https://partner.alfabank.ru/public-api/v2/leads").mock(
        return_value=httpx.Response(200, json=[
            {"uuid": "x", "state": "SENT", "productInfo": []}
        ])
    )
    dt_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dt_to = datetime(2026, 1, 2, tzinfo=timezone.utc)
    items = client.leads.list(
        page=2, per_page=50, from_date=dt_from, to_date=dt_to,
        filter_date_by="modifiedAt",
    )
    assert len(items) == 1
    request = route.calls.last.request
    qs = dict(request.url.params)
    assert qs["page"] == "2"
    assert qs["perPage"] == "50"
    assert qs["filterDateBy"] == "modifiedAt"
    # 2026-01-01 UTC == 1767225600000 ms
    assert qs["fromDate"] == str(int(dt_from.timestamp() * 1000))
    assert qs["toDate"] == str(int(dt_to.timestamp() * 1000))


def test_list_per_page_limit(client: AlfaBankClient) -> None:
    with pytest.raises(ValueError):
        client.leads.list(page=1, per_page=101)


@respx.mock
def test_get_with_deal_fin_amount(
    client: AlfaBankClient, fixture_loader: object,
) -> None:
    payload = fixture_loader("lead_info_with_deal_amount.json")  # type: ignore[operator]
    respx.get("https://partner.alfabank.ru/public-api/v2/leads/demo").mock(
        return_value=httpx.Response(200, json=payload)
    )
    info = client.leads.get("demo")
    loan = next(
        p for p in info.product_info if p.product_code == ProductCode.LP_LOAN_BUS
    )
    assert loan.product_params is not None
    assert loan.product_params.deal_fin_amount == 5_000_000
