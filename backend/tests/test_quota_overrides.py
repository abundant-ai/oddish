"""S4 quota overrides: admin PUT /quotas/{user_id} + effective-limit COALESCE.

The migration test is static (no DB). The PUT round-trips use dependency_overrides
for the admin user-auth; the FULL-API-key rejection uses a real key.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import require_can_manage_quotas
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, OrganizationModel, UserModel, UserRole
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


# --- S4-T1: the migration creates the table and has no data statements ----------


def test_quotas_migration_is_ddl_only():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "add_quotas_table_001.py"
    )
    source = migration.read_text()
    assert "CREATE TABLE IF NOT EXISTS quotas" in source
    assert "UNIQUE (org_id, user_id)" in source
    assert "DROP TABLE IF EXISTS quotas" in source

    uppercased = source.upper()
    assert "INSERT INTO" not in uppercased
    assert "UPDATE QUOTAS" not in uppercased


# --- shared fixture: org + admin + one member ----------------------------------


@pytest_asyncio.fixture
async def org_with_member():
    org_id = f"org_ov_{uuid.uuid4().hex[:8]}"
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
            from models import QuotaModel

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


# --- S4-T2: PUT a value overrides the default and shows on GET -------------------


@requires_db
@pytest.mark.asyncio
async def test_put_override_reflects_in_admin_list(org_with_member):
    org_id, admin_user, member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            put_response = await client.put(
                f"/quotas/{member_a.id}", json={"limit_usd": "3.50"}
            )
            assert put_response.status_code == 200
            assert put_response.json()["limit_usd"] == pytest.approx(3.5)

            list_response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    members_by_id = {m["user_id"]: m for m in list_response.json()["members"]}
    assert members_by_id[member_a.id]["limit_usd"] == pytest.approx(3.5)


# --- S4-T3: PUT null clears the override; the member reverts to the default ------


@requires_db
@pytest.mark.asyncio
async def test_put_null_clears_override_back_to_default(org_with_member):
    org_id, admin_user, member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            await client.put(f"/quotas/{member_a.id}", json={"limit_usd": "3.50"})
            clear_response = await client.put(
                f"/quotas/{member_a.id}", json={"limit_usd": None}
            )
            assert clear_response.status_code == 200
            assert clear_response.json()["limit_usd"] == pytest.approx(100.0)

            list_response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    members_by_id = {m["user_id"]: m for m in list_response.json()["members"]}
    assert members_by_id[member_a.id]["limit_usd"] == pytest.approx(100.0)


# --- S4-T4: cross-org PUT 404s; a FULL-scope API key is rejected -----------------


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_limit", ["100000000", "-5.00", "0", "3.123456"])
async def test_put_rejects_out_of_range_or_negative_limit(org_with_member, invalid_limit):
    org_id, admin_user, member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.put(
                f"/quotas/{member_a.id}", json={"limit_usd": invalid_limit}
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


@requires_db
@pytest.mark.asyncio
async def test_put_cross_org_user_is_404(org_with_member):
    org_id, admin_user, _member_a = org_with_member
    other_org_id = f"org_other_{uuid.uuid4().hex[:8]}"
    outsider = _member(other_org_id, "outsider", "member")
    async with get_session() as session:
        session.add(OrganizationModel(id=other_org_id, name=other_org_id, slug=other_org_id))
        await session.flush()
        session.add(outsider)

    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.put(
                f"/quotas/{outsider.id}", json={"limit_usd": "1.00"}
            )
            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == outsider.id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == other_org_id
                )
            )


@pytest_asyncio.fixture
async def org_with_full_key():
    org_id = f"org_ovk_{uuid.uuid4().hex[:8]}"
    member_a = _member(org_id, "membera", "member")
    key_model, raw_key = create_api_key(
        org_id=org_id, name="ovk", scope=APIKeyScope.FULL
    )
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(member_a)
        session.add(key_model)
    try:
        yield raw_key, member_a
    finally:
        from oddish.db.models import APIKeyModel

        async with get_session() as session:
            await session.execute(
                APIKeyModel.__table__.delete().where(APIKeyModel.id == key_model.id)
            )
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == member_a.id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_put_quota_rejects_full_api_key(org_with_full_key):
    raw_key, member_a = org_with_full_key
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/quotas/{member_a.id}",
            json={"limit_usd": "5.00"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert response.status_code == 403


# --- soft-deleted overrides never enforce ---------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_effective_limit_ignores_soft_deleted_override(org_with_member):
    """A soft-deleted override row must not enforce a stale limit: the payer
    falls back to the default, and reviving the row restores the override."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from models import QuotaModel
    from oddish.config import settings
    from oddish.core.quotas import get_effective_limit

    org_id, _admin, member_a = org_with_member
    async with get_session() as session:
        override = QuotaModel(
            org_id=org_id,
            user_id=member_a.id,
            limit_usd=Decimal("3.00"),
            deleted_at=datetime.now(timezone.utc),
        )
        session.add(override)
        await session.flush()

        # Tombstoned override is invisible -> configured default, never the 3.00.
        soft_deleted = await get_effective_limit(session, org_id, member_a.id)
        assert soft_deleted == settings.default_daily_quota_usd
        assert soft_deleted != Decimal("3.00")

        # Reviving the same row (clearing the tombstone) restores the override.
        override.deleted_at = None
        await session.flush()
        revived = await get_effective_limit(session, org_id, member_a.id)
        assert revived == Decimal("3.00")


@requires_db
@pytest.mark.asyncio
async def test_put_revives_soft_deleted_override(org_with_member):
    """A PUT over a tombstoned override must revive it: the (org_id, user_id)
    unique index spans all rows, so the upsert conflicts with the dead row and
    must clear deleted_at or the new limit stores but never enforces."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from models import QuotaModel

    org_id, admin_user, member_a = org_with_member
    async with get_session() as session:
        session.add(
            QuotaModel(
                org_id=org_id,
                user_id=member_a.id,
                limit_usd=Decimal("1.00"),
                deleted_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()

    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            put_response = await client.put(
                f"/quotas/{member_a.id}", json={"limit_usd": "4.00"}
            )
            assert put_response.status_code == 200
            # Enforced (revived), not the tombstoned 1.00 nor the 100.00 default.
            assert put_response.json()["limit_usd"] == pytest.approx(4.0)
            list_response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    members_by_id = {m["user_id"]: m for m in list_response.json()["members"]}
    assert members_by_id[member_a.id]["limit_usd"] == pytest.approx(4.0)
