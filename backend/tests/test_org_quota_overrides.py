"""Org-level aggregate daily cap: admin PUT /quotas/org + GET /quotas org fields.

The auth-dependency tests run without a DB. The PUT/GET round-trips use
dependency_overrides for admin user-auth; the API-key rejection uses a real key.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import require_can_manage_quotas
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, OrganizationModel, UserModel, UserRole
from oddish.config import settings
from oddish.core.api_keys import create_api_key
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _user_auth(*, org_id: str, user_id: str, role: UserRole) -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=org_id, user_id=user_id, user_role=role
    )


def _member(org_id: str, handle: str, role: str) -> UserModel:
    suffix = uuid.uuid4().hex[:8]
    return UserModel(
        id=f"user_{handle}_{suffix}",
        org_id=org_id,
        email=f"{handle}_{suffix}@example.com",
        github_username=handle,
        clerk_user_id=f"clerk_{suffix}",
        role=role,
        is_active=True,
    )


# --- migration is DDL-only, single live row per org ----------------------------


def test_org_quotas_migration_is_ddl_only():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "add_org_quotas_001.py"
    )
    source = migration.read_text()
    assert "CREATE TABLE IF NOT EXISTS org_quotas" in source
    assert "uq_org_quotas_org" in source
    assert "WHERE deleted_at IS NULL" in source
    assert "DROP TABLE IF EXISTS org_quotas" in source

    uppercased = source.upper()
    assert "INSERT INTO" not in uppercased


# --- shared fixture: org + admin + member --------------------------------------


@pytest_asyncio.fixture
async def org_with_member():
    org_id = f"org_oq_{uuid.uuid4().hex[:8]}"
    admin_user = _member(org_id, "admin", "admin")
    member_a = _member(org_id, "membera", "member")
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add_all([admin_user, member_a])
    try:
        yield org_id, admin_user, member_a
    finally:
        async with get_session() as session:
            from models import OrgQuotaModel, QuotaModel

            await session.execute(
                OrgQuotaModel.__table__.delete().where(OrgQuotaModel.org_id == org_id)
            )
            await session.execute(
                QuotaModel.__table__.delete().where(QuotaModel.org_id == org_id)
            )
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.id.in_([admin_user.id, member_a.id])
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


def _admin_client(app, org_id, admin_user):
    app.dependency_overrides[require_can_manage_quotas] = lambda: _user_auth(
        org_id=org_id, user_id=admin_user.id, role=UserRole.ADMIN
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- PUT sets an org override; GET /quotas reflects it --------------------------


@requires_db
@pytest.mark.asyncio
async def test_put_org_override_reflects_in_get(org_with_member):
    org_id, admin_user, _member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            put_response = await client.put("/quotas/org", json={"limit_usd": "42.00"})
            assert put_response.status_code == 200
            assert put_response.json()["org_limit_usd"] == pytest.approx(42.0)

            list_response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    body = list_response.json()
    assert body["org_limit_usd"] == pytest.approx(42.0)
    assert body["org_used_usd"] == pytest.approx(0.0)
    assert "org_reserved_usd" in body


# --- GET /quotas survives a missing org_quotas table (deploy-before-migrate) ----


@requires_db
@pytest.mark.asyncio
async def test_get_quotas_degrades_when_org_quotas_schema_missing(
    org_with_member, monkeypatch
):
    """Deploy-before-migrate window: the startup guard forces quota_mode=off
    but GET /quotas must not 500 -- it degrades to no-org-cap display fields
    while the member list still renders."""
    from sqlalchemy.exc import ProgrammingError

    import api.routers.orgs as orgs_mod

    async def raise_undefined_table(session, org_id):
        raise ProgrammingError(
            "SELECT limit_usd FROM org_quotas", {}, Exception("UndefinedTableError")
        )

    monkeypatch.setattr(orgs_mod, "get_effective_org_limit", raise_undefined_table)
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", None)

    org_id, admin_user, member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert {m["user_id"] for m in body["members"]} == {admin_user.id, member_a.id}
    assert body["org_limit_usd"] is None
    assert body["org_used_usd"] == pytest.approx(0.0)
    assert body["org_reserved_usd"] == pytest.approx(0.0)
    assert body["org_default_limit_usd"] is None


# --- upsert: a second PUT overwrites the existing override (one live row) -------


@requires_db
@pytest.mark.asyncio
async def test_put_org_override_upserts(org_with_member):
    org_id, admin_user, _member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            await client.put("/quotas/org", json={"limit_usd": "10.00"})
            second = await client.put("/quotas/org", json={"limit_usd": "20.00"})
            assert second.status_code == 200
            assert second.json()["org_limit_usd"] == pytest.approx(20.0)
    finally:
        app.dependency_overrides.clear()

    async with get_session() as session:
        from models import OrgQuotaModel
        from sqlalchemy import func, select

        live_rows = await session.scalar(
            select(func.count())
            .select_from(OrgQuotaModel)
            .where(
                OrgQuotaModel.org_id == org_id,
                OrgQuotaModel.deleted_at.is_(None),
            )
        )
    assert live_rows == 1


# --- clear: PUT null removes the override; falls back to the default ------------


@requires_db
@pytest.mark.asyncio
async def test_put_org_null_clears_override(org_with_member, monkeypatch):
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", None)
    org_id, admin_user, _member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            await client.put("/quotas/org", json={"limit_usd": "10.00"})
            clear = await client.put("/quotas/org", json={"limit_usd": None})
            assert clear.status_code == 200
            # No default configured -> no org cap after clearing.
            assert clear.json()["org_limit_usd"] is None
            list_response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    assert list_response.json()["org_limit_usd"] is None


@requires_db
@pytest.mark.asyncio
async def test_get_quotas_reports_configured_default(org_with_member, monkeypatch):
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("77.00"))
    org_id, admin_user, _member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            list_response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    body = list_response.json()
    # No override row -> effective cap is the configured default.
    assert body["org_limit_usd"] == pytest.approx(77.0)
    assert body["org_default_limit_usd"] == pytest.approx(77.0)


# --- validation: out-of-range/negative rejected --------------------------------


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_limit", ["100000000", "-5.00", "0", "3.123456"])
async def test_put_org_rejects_out_of_range(org_with_member, invalid_limit):
    org_id, admin_user, _member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.put(
                "/quotas/org", json={"limit_usd": invalid_limit}
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


# --- auth: a member (non-admin) is rejected ------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_put_org_rejects_member(org_with_member):
    org_id, _admin_user, member_a = org_with_member
    app = create_app()
    # Override the underlying auth (not require_can_manage_quotas) so the real
    # admin-only gate runs against a MEMBER and rejects.
    from auth import require_auth

    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member_a.id, role=UserRole.MEMBER
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put("/quotas/org", json={"limit_usd": "5.00"})
            assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


# --- auth: a FULL-scope API key is rejected ------------------------------------


@pytest_asyncio.fixture
async def org_with_full_key():
    org_id = f"org_oqk_{uuid.uuid4().hex[:8]}"
    key_model, raw_key = create_api_key(
        org_id=org_id, name="oqk", scope=APIKeyScope.FULL
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
async def test_put_org_rejects_full_api_key(org_with_full_key):
    raw_key = org_with_full_key
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/quotas/org",
            json={"limit_usd": "5.00"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert response.status_code == 403
