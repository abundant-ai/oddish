"""Unit tests for the Cursor Admin API billing client.

Uses httpx.MockTransport so no real key or network is needed; asserts on the
request (auth, body, path) and on parsing/aggregation of the response.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from oddish.cursor_billing import (
    CURSOR_API_BASE,
    CursorBillingError,
    fetch_all_team_spend,
    fetch_team_spend,
)

_PAGE_1 = {
    "teamMemberSpend": [
        {
            "userId": 1,
            "name": "Alice",
            "email": "alice@co.com",
            "role": "admin",
            "spendCents": 1234,
            "overallSpendCents": 5678,
            "fastPremiumRequests": 42,
        },
        {
            "userId": 2,
            "name": "Bob",
            "email": "bob@co.com",
            "role": "member",
            "spendCents": 0,
            "overallSpendCents": 2000,
            "fastPremiumRequests": 0,
        },
    ],
    "subscriptionCycleStart": 1710720000000,
    "totalMembers": 3,
    "totalPages": 2,
}

_PAGE_2 = {
    "teamMemberSpend": [
        {
            "userId": 3,
            "name": "Carol",
            "email": "carol@co.com",
            "role": "member",
            "spendCents": 100,
            "overallSpendCents": 100,
            "fastPremiumRequests": 1,
        }
    ],
    "subscriptionCycleStart": 1710720000000,
    "totalMembers": 3,
    "totalPages": 2,
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_team_spend_parses_and_authenticates() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_PAGE_1)

    spend = fetch_team_spend(
        "secret-key", page=1, page_size=25, client=_client(handler)
    )

    # hits the right endpoint
    assert captured["url"] == f"{CURSOR_API_BASE}/teams/spend"
    # basic auth = key as username, empty password
    expected = base64.b64encode(b"secret-key:").decode()
    assert captured["auth"] == f"Basic {expected}"
    assert captured["body"] == {"page": 1, "pageSize": 25}

    # parsing + dollar conversion
    assert spend.total_members == 3
    assert spend.total_pages == 2
    assert spend.members[0].email == "alice@co.com"
    assert spend.members[0].spend_usd == pytest.approx(12.34)
    assert spend.members[0].overall_spend_usd == pytest.approx(56.78)
    # aggregates over the returned page
    assert spend.total_overall_spend_usd == pytest.approx(76.78)
    assert spend.total_on_demand_spend_usd == pytest.approx(12.34)


def test_optional_filters_included_in_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_PAGE_1)

    fetch_team_spend(
        "k",
        search_term="alice@co.com",
        sort_by="amount",
        sort_direction="desc",
        client=_client(handler),
    )
    assert captured["body"] == {
        "page": 1,
        "searchTerm": "alice@co.com",
        "sortBy": "amount",
        "sortDirection": "desc",
    }


def test_overall_spend_falls_back_to_spend_cents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "teamMemberSpend": [
                    {"userId": 9, "email": "x@co.com", "spendCents": 500}
                ],
                "totalMembers": 1,
                "totalPages": 1,
            },
        )

    spend = fetch_team_spend("k", client=_client(handler))
    # overallSpendCents missing -> fall back to spendCents
    assert spend.members[0].overall_spend_cents == 500
    assert spend.total_overall_spend_usd == pytest.approx(5.0)


def test_fetch_all_team_spend_paginates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        return httpx.Response(200, json=_PAGE_1 if page == 1 else _PAGE_2)

    spend = fetch_all_team_spend("k", page_size=2, client=_client(handler))
    # merged across both pages
    assert [m.email for m in spend.members] == [
        "alice@co.com",
        "bob@co.com",
        "carol@co.com",
    ]
    assert spend.total_overall_spend_usd == pytest.approx(77.78)


def test_missing_key_raises() -> None:
    with pytest.raises(CursorBillingError, match="CURSOR_ADMIN_API_KEY"):
        fetch_team_spend(None)


def test_http_error_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(CursorBillingError, match="request failed"):
        fetch_team_spend("bad-key", client=_client(handler))
