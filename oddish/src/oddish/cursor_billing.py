"""Actual Cursor team billing from the Cursor Admin API.

The per-trial cursor-cli cost surfaced elsewhere in oddish is *estimated* from
token counts whenever the CLI omits a dollar figure (see ``model_pricing`` and
``_resolve_browse_trial_cost``). This module is the authoritative alternative:
it pulls real spend from Cursor's Admin API, reported in cents per team member
for the current subscription cycle.

  POST https://api.cursor.com/teams/spend
  Auth: HTTP Basic, admin API key as username with an empty password.

Requires a Cursor *team admin* API key (``CURSOR_ADMIN_API_KEY``). Docs:
https://cursor.com/docs/account/teams/admin-api
"""

from __future__ import annotations

import dataclasses

import httpx

CURSOR_API_BASE = "https://api.cursor.com"
_SPEND_PATH = "/teams/spend"
_DEFAULT_TIMEOUT = 30.0


class CursorBillingError(RuntimeError):
    """Raised when the Cursor Admin API key is missing or a request fails."""


@dataclasses.dataclass(frozen=True)
class MemberSpend:
    """One team member's spend for the current subscription cycle."""

    user_id: int
    name: str
    email: str
    role: str
    # On-demand (usage-based) spend only, in cents.
    spend_cents: int
    # Total spend including plan-included usage, in cents.
    overall_spend_cents: int
    fast_premium_requests: int

    @property
    def spend_usd(self) -> float:
        return self.spend_cents / 100.0

    @property
    def overall_spend_usd(self) -> float:
        return self.overall_spend_cents / 100.0


@dataclasses.dataclass(frozen=True)
class TeamSpend:
    """Aggregated actual spend for a team, current subscription cycle."""

    members: list[MemberSpend]
    subscription_cycle_start_ms: int
    total_members: int
    total_pages: int

    @property
    def total_overall_spend_usd(self) -> float:
        """Total actual spend (incl. included usage) across returned members."""
        return sum(m.overall_spend_cents for m in self.members) / 100.0

    @property
    def total_on_demand_spend_usd(self) -> float:
        """Total on-demand (usage-based) spend across returned members."""
        return sum(m.spend_cents for m in self.members) / 100.0


def _parse_spend(data: dict) -> TeamSpend:
    members = [
        MemberSpend(
            user_id=int(m.get("userId") or 0),
            name=str(m.get("name") or ""),
            email=str(m.get("email") or ""),
            role=str(m.get("role") or ""),
            spend_cents=int(m.get("spendCents") or 0),
            overall_spend_cents=int(
                m.get("overallSpendCents", m.get("spendCents")) or 0
            ),
            fast_premium_requests=int(m.get("fastPremiumRequests") or 0),
        )
        for m in (data.get("teamMemberSpend") or [])
    ]
    return TeamSpend(
        members=members,
        subscription_cycle_start_ms=int(data.get("subscriptionCycleStart") or 0),
        total_members=int(data.get("totalMembers") or 0),
        total_pages=int(data.get("totalPages") or 0),
    )


def fetch_team_spend(
    api_key: str | None,
    *,
    search_term: str | None = None,
    sort_by: str | None = None,
    sort_direction: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> TeamSpend:
    """Fetch one page of actual team spend from the Cursor Admin API.

    Raises ``CursorBillingError`` if ``api_key`` is missing or the request
    fails. Pass ``client`` to reuse a connection or to inject a test transport.
    """
    if not api_key:
        raise CursorBillingError(
            "CURSOR_ADMIN_API_KEY is not set; create a Cursor team admin API key."
        )
    body: dict[str, object] = {"page": page}
    if search_term:
        body["searchTerm"] = search_term
    if sort_by:
        body["sortBy"] = sort_by
    if sort_direction:
        body["sortDirection"] = sort_direction
    if page_size is not None:
        body["pageSize"] = page_size

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(
            f"{CURSOR_API_BASE}{_SPEND_PATH}", json=body, auth=(api_key, "")
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise CursorBillingError(f"Cursor Admin API request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()
    return _parse_spend(data)


def fetch_all_team_spend(
    api_key: str | None,
    *,
    search_term: str | None = None,
    sort_by: str | None = None,
    sort_direction: str | None = None,
    page_size: int = 100,
    timeout: float = _DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> TeamSpend:
    """Fetch every page of team spend and return one merged ``TeamSpend``."""
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        first = fetch_team_spend(
            api_key,
            search_term=search_term,
            sort_by=sort_by,
            sort_direction=sort_direction,
            page=1,
            page_size=page_size,
            client=client,
        )
        members = list(first.members)
        for page in range(2, first.total_pages + 1):
            members.extend(
                fetch_team_spend(
                    api_key,
                    search_term=search_term,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                    page=page,
                    page_size=page_size,
                    client=client,
                ).members
            )
    finally:
        if owns_client:
            client.close()
    return dataclasses.replace(first, members=members)
