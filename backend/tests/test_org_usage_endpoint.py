"""GET /quotas/org: member-visible org budget snapshot + daily-goal math.

The goal-math cases exercise the pure ``_org_usage_response`` helper with a
pinned "today" so ``days_remaining`` and the adaptive goal are deterministic.
The endpoint cases (auth, no-cap, degrade) run against a real app.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import api.routers.orgs as orgs_mod
from api.app import create_app
from api.routers.orgs import _org_usage_response
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, OrganizationModel, UserModel, UserRole
from oddish.config import QuotaMode, settings
from oddish.core.api_keys import create_api_key
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _fields(*, limit, used_month=0.0, reserved=0.0, default=None) -> dict:
    return {
        "org_limit_usd": limit,
        "org_used_usd": used_month,
        "org_reserved_usd": reserved,
        "org_default_limit_usd": default,
    }


def _pin_today(monkeypatch, dt: datetime) -> None:
    monkeypatch.setattr(orgs_mod, "start_of_today_utc", lambda now=None: dt)


# --- goal math (pure) ----------------------------------------------------------


def test_goal_null_when_no_cap(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    resp = _org_usage_response(_fields(limit=None, used_month=5.0, reserved=3.0), Decimal("2.0"))
    assert resp.org_limit_usd is None
    assert resp.daily_goal_usd is None
    # Non-cap fields still populated.
    assert resp.org_used_month_usd == pytest.approx(5.0)
    assert resp.org_reserved_usd == pytest.approx(3.0)
    assert resp.org_used_today_usd == pytest.approx(2.0)
    assert resp.days_remaining == 26  # 31 - 6 + 1, includes today


def test_goal_is_budget_over_days_remaining_when_no_spend(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    resp = _org_usage_response(_fields(limit=300.0, used_month=0.0), Decimal("0"))
    assert resp.days_remaining == 26
    assert resp.daily_goal_usd == pytest.approx(300.0 / 26)


def test_overspend_early_in_month_shrinks_goal(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    # $200 already booked BEFORE today (used_today=0) of a $300 cap.
    resp = _org_usage_response(_fields(limit=300.0, used_month=200.0), Decimal("0"))
    assert resp.daily_goal_usd == pytest.approx(100.0 / 26)
    # Strictly smaller than the fresh-budget goal.
    assert resp.daily_goal_usd < 300.0 / 26


def test_todays_spend_does_not_inflate_goal(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    # $250 spent, but ALL of it today -> nothing booked before today.
    resp = _org_usage_response(_fields(limit=300.0, used_month=250.0), Decimal("250.0"))
    assert resp.daily_goal_usd == pytest.approx(300.0 / 26)


def test_goal_floors_at_zero_when_budget_exhausted(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    # $400 booked before today against a $300 cap -> no goal left, floored at 0.
    resp = _org_usage_response(_fields(limit=300.0, used_month=400.0), Decimal("0"))
    assert resp.daily_goal_usd == pytest.approx(0.0)


def test_goal_subtracts_in_flight_reservation(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    # $100 settled before today + $50 in-flight reserved of a $300 cap ->
    # uncommitted budget is $150 spread over 26 days.
    resp = _org_usage_response(
        _fields(limit=300.0, used_month=100.0, reserved=50.0), Decimal("0")
    )
    assert resp.daily_goal_usd == pytest.approx(150.0 / 26)


def test_goal_floors_at_zero_when_reservation_exhausts_budget(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    # settled + reserved already at the cap -> goal 0, matching the 402 gate.
    resp = _org_usage_response(
        _fields(limit=300.0, used_month=250.0, reserved=60.0), Decimal("0")
    )
    assert resp.daily_goal_usd == pytest.approx(0.0)


def test_days_remaining_on_last_day_of_month(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 31, tzinfo=timezone.utc))
    resp = _org_usage_response(_fields(limit=300.0, used_month=100.0), Decimal("0"))
    assert resp.days_remaining == 1  # only today left
    assert resp.daily_goal_usd == pytest.approx(200.0)


def test_days_remaining_on_first_of_month(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 2, 1, tzinfo=timezone.utc))
    resp = _org_usage_response(_fields(limit=280.0, used_month=0.0), Decimal("0"))
    assert resp.days_remaining == 28  # non-leap February
    assert resp.daily_goal_usd == pytest.approx(10.0)


def test_enforced_reflects_quota_mode(monkeypatch):
    _pin_today(monkeypatch, datetime(2026, 7, 6, tzinfo=timezone.utc))
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    assert _org_usage_response(_fields(limit=None), Decimal("0")).enforced is True
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    assert _org_usage_response(_fields(limit=None), Decimal("0")).enforced is False


# --- endpoint: auth + no-cap + degrade -----------------------------------------


def _user_auth(*, org_id, user_id, role) -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=org_id, user_id=user_id, user_role=role
    )


@pytest_asyncio.fixture
async def org_with_member():
    org_id = f"org_ou_{uuid.uuid4().hex[:8]}"
    member = UserModel(
        id=f"user_m_{uuid.uuid4().hex[:8]}",
        org_id=org_id,
        email=f"m_{uuid.uuid4().hex[:6]}@example.com",
        github_username="m",
        clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
        role=UserRole.MEMBER,
        is_active=True,
    )
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(member)
    try:
        yield org_id, member
    finally:
        async with get_session() as session:
            from models import OrgQuotaModel

            await session.execute(
                OrgQuotaModel.__table__.delete().where(OrgQuotaModel.org_id == org_id)
            )
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == member.id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_any_member_gets_no_cap_snapshot(org_with_member, monkeypatch):
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", None)
    org_id, member = org_with_member
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member.id, role=UserRole.MEMBER
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/quotas/org")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["org_limit_usd"] is None
    assert body["daily_goal_usd"] is None
    assert body["days_remaining"] >= 1
    assert "org_used_month_usd" in body
    assert "org_used_today_usd" in body
    assert body["enforced"] == (settings.quota_mode == QuotaMode.ENFORCE)


@requires_db
@pytest.mark.asyncio
async def test_member_sees_configured_default_cap(org_with_member, monkeypatch):
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", Decimal("500.00"))
    org_id, member = org_with_member
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member.id, role=UserRole.MEMBER
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/quotas/org")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["org_limit_usd"] == pytest.approx(500.0)
    assert body["daily_goal_usd"] is not None


@requires_db
@pytest.mark.asyncio
async def test_org_usage_degrades_when_schema_missing(org_with_member, monkeypatch):
    from sqlalchemy.exc import ProgrammingError

    class FakeUndefinedTable(Exception):
        sqlstate = "42P01"

    async def raise_undefined_table(session, org_id):
        raise ProgrammingError(
            "SELECT limit_usd FROM org_quotas", {}, FakeUndefinedTable()
        )

    monkeypatch.setattr(orgs_mod, "get_effective_org_limit", raise_undefined_table)
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", None)

    org_id, member = org_with_member
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member.id, role=UserRole.MEMBER
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/quotas/org")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["org_limit_usd"] is None
    assert body["daily_goal_usd"] is None
    assert body["org_used_month_usd"] == pytest.approx(0.0)


@requires_db
@pytest.mark.asyncio
async def test_org_usage_does_not_swallow_other_programming_errors(
    org_with_member, monkeypatch
):
    """The degrade path is ONLY for undefined-table (42P01). Any other
    ProgrammingError is a real bug and must surface, not be masked as a
    no-cap snapshot."""
    from sqlalchemy.exc import ProgrammingError

    async def raise_other_programming_error(session, org_id):
        raise ProgrammingError("SELECT broken syntax", {}, Exception("SyntaxError"))

    monkeypatch.setattr(
        orgs_mod, "get_effective_org_limit", raise_other_programming_error
    )

    org_id, member = org_with_member
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member.id, role=UserRole.MEMBER
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            with pytest.raises(ProgrammingError):
                await client.get("/quotas/org")
    finally:
        app.dependency_overrides.clear()


@requires_db
@pytest.mark.asyncio
async def test_org_usage_degrade_preserves_real_trial_usage(
    org_with_member, monkeypatch
):
    """org_quotas missing degrades the CAP to the default, but month spend and
    reservation live on trials and must still be reported (not zeroed)."""
    from sqlalchemy.exc import ProgrammingError

    class FakeUndefinedTable(Exception):
        sqlstate = "42P01"

    async def raise_undefined_table(session, org_id):
        raise ProgrammingError(
            "SELECT limit_usd FROM org_quotas", {}, FakeUndefinedTable()
        )

    async def fake_usage(session, org_id):
        return Decimal("12.34"), Decimal("5.00")

    monkeypatch.setattr(orgs_mod, "get_effective_org_limit", raise_undefined_table)
    monkeypatch.setattr(orgs_mod, "_org_trial_usage", fake_usage)
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", Decimal("300.00"))

    org_id, member = org_with_member
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member.id, role=UserRole.MEMBER
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/quotas/org")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["org_limit_usd"] == pytest.approx(300.0)
    assert body["org_used_month_usd"] == pytest.approx(12.34)
    assert body["org_reserved_usd"] == pytest.approx(5.0)


@requires_db
@pytest.mark.asyncio
async def test_put_org_quota_degrades_503_when_schema_missing(
    org_with_member, monkeypatch
):
    """PUT can't write a cap to a missing org_quotas table; it must return a
    clean 503, never an uncaught 500."""
    from sqlalchemy.exc import ProgrammingError

    from auth import require_can_manage_quotas

    class FakeUndefinedTable(Exception):
        sqlstate = "42P01"

    async def raise_undefined_table(session, org_id):
        raise ProgrammingError(
            "SELECT limit_usd FROM org_quotas", {}, FakeUndefinedTable()
        )

    monkeypatch.setattr(orgs_mod, "get_effective_org_limit", raise_undefined_table)

    org_id, member = org_with_member
    app = create_app()
    app.dependency_overrides[require_can_manage_quotas] = lambda: _user_auth(
        org_id=org_id, user_id=member.id, role=UserRole.ADMIN
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put("/quotas/org", json={"limit_usd": None})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503


# --- API key auth: consistent with GET /quotas/me (a valid key gets a snapshot) -


@pytest_asyncio.fixture
async def org_with_full_key():
    org_id = f"org_ouk_{uuid.uuid4().hex[:8]}"
    key_model, raw_key = create_api_key(
        org_id=org_id, name="ouk", scope=APIKeyScope.FULL
    )
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(key_model)
    try:
        yield raw_key
    finally:
        from oddish.db.models import APIKeyModel

        async with get_session() as session:
            await session.execute(
                APIKeyModel.__table__.delete().where(APIKeyModel.id == key_model.id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_org_usage_allows_api_key(org_with_full_key, monkeypatch):
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", None)
    raw_key = org_with_full_key
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/quotas/org", headers={"Authorization": f"Bearer {raw_key}"}
        )
    # A valid API key resolves an org context, so the member-visible snapshot is
    # returned (consistent with GET /quotas/me, which also serves API keys).
    assert response.status_code == 200
    assert response.json()["org_limit_usd"] is None
