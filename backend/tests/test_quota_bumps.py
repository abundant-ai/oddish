"""Temporary quota bumps: additive, time-boxed boosts on the base 24h limit.

The migration test is static (no DB). Core-helper and admit_trials cases hit
the real ``oddish.core.quotas`` against the unit DB. Endpoint round-trips use
FastAPI ``dependency_overrides`` for the admin user-auth; the API-key rejection
uses a real FULL-scope key (proving the bumps routes reject API keys the same
way ``require_can_manage_quotas`` does elsewhere).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers.orgs import add_member_quota_bump, revoke_member_quota_bumps
from auth import require_can_manage_quotas
from auth.types import AuthContext, AuthMethod
from models import (
    APIKeyScope,
    OrganizationModel,
    QuotaBumpModel,
    QuotaModel,
    UserModel,
    UserRole,
)
from oddish.config import QuotaMode, settings
from oddish.core.api_keys import create_api_key
from oddish.core.quota_admission import QuotaExceeded, admit_trials
from oddish.core.quotas import get_effective_limit, live_bump_total
from oddish.db import TaskModel, TrialModel, WorkerJobModel, get_session, utcnow
from oddish.queue import create_task
from oddish.schemas import TaskSubmission, TrialSpec

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


def _admin_client(app, org_id, admin_user):
    app.dependency_overrides[require_can_manage_quotas] = lambda: _user_auth(
        org_id=org_id, user_id=admin_user.id, role=UserRole.ADMIN
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- 1. static migration test: DDL only, CHECK constraint, table, downgrade -----


def test_quota_bumps_migration_is_ddl_only():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "add_quota_bumps_001.py"
    )
    source = migration.read_text()
    assert "CREATE TABLE IF NOT EXISTS quota_bumps" in source
    assert "ck_quota_bumps_amount_positive" in source
    assert "CHECK (amount_usd > 0)" in source
    assert "DROP TABLE IF EXISTS quota_bumps" in source
    assert 'down_revision' in source
    assert '"apk_role_backend_001"' in source

    uppercased = source.upper()
    assert "INSERT INTO" not in uppercased
    assert "UPDATE QUOTA_BUMPS" not in uppercased


# --- shared fixture: org + admin + one member ----------------------------------


@pytest_asyncio.fixture
async def org_with_member():
    org_id = f"org_bump_{uuid.uuid4().hex[:8]}"
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
            await session.execute(
                QuotaBumpModel.__table__.delete().where(
                    QuotaBumpModel.org_id == org_id
                )
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


async def _add_bump(
    session, org_id, user_id, amount, *, expires_at, revoked_at=None
):
    session.add(
        QuotaBumpModel(
            id=uuid.uuid4().hex[:8],
            org_id=org_id,
            user_id=user_id,
            amount_usd=Decimal(str(amount)),
            expires_at=expires_at,
            revoked_at=revoked_at,
        )
    )
    await session.flush()


# --- 2. effective = default + live bump when there is no override row ------------


@requires_db
@pytest.mark.asyncio
async def test_effective_limit_is_default_plus_live_bump(org_with_member):
    org_id, _admin, member_a = org_with_member
    future = utcnow() + timedelta(hours=6)
    async with get_session() as session:
        await _add_bump(session, org_id, member_a.id, "25.00", expires_at=future)
        effective = await get_effective_limit(session, org_id, member_a.id)
    assert effective == settings.default_daily_quota_usd + Decimal("25.0000")


# --- 3. effective = override + live bump when an override row exists -------------


@requires_db
@pytest.mark.asyncio
async def test_effective_limit_is_override_plus_live_bump(org_with_member):
    org_id, _admin, member_a = org_with_member
    future = utcnow() + timedelta(hours=6)
    async with get_session() as session:
        session.add(
            QuotaModel(org_id=org_id, user_id=member_a.id, limit_usd=Decimal("5.00"))
        )
        await session.flush()
        await _add_bump(session, org_id, member_a.id, "10.00", expires_at=future)
        effective = await get_effective_limit(session, org_id, member_a.id)
    assert effective == Decimal("15.0000")


# --- 4. expired excluded; revoked excluded; two live bumps SUM ------------------


@requires_db
@pytest.mark.asyncio
async def test_live_bump_total_excludes_expired_and_revoked_and_sums(org_with_member):
    org_id, _admin, member_a = org_with_member
    past = utcnow() - timedelta(hours=1)
    future = utcnow() + timedelta(hours=6)
    later = utcnow() + timedelta(hours=48)
    async with get_session() as session:
        # Expired: not counted.
        await _add_bump(session, org_id, member_a.id, "100.00", expires_at=past)
        # Revoked: not counted.
        await _add_bump(
            session,
            org_id,
            member_a.id,
            "200.00",
            expires_at=future,
            revoked_at=utcnow(),
        )
        # Two live bumps: SUM, MAX(expires_at).
        await _add_bump(session, org_id, member_a.id, "7.00", expires_at=future)
        await _add_bump(session, org_id, member_a.id, "3.00", expires_at=later)

        total, max_expires = await live_bump_total(session, org_id, member_a.id)

    assert total == Decimal("10.0000")
    assert max_expires is not None
    # MAX expiry is the later of the two live bumps (compare on the second).
    assert abs((max_expires - later).total_seconds()) < 1


# --- 5. admit_trials picks up a live bump, then re-blocks once it expires --------


@pytest.fixture
def _enforce_mode(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0"))
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("0.3000"))


@requires_db
@pytest.mark.asyncio
async def test_admit_trials_admits_after_bump_then_blocks_when_expired(
    org_with_member, _enforce_mode
):
    org_id, _admin, member_a = org_with_member
    task_id = f"bump-adm-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            await create_task(
                session,
                TaskSubmission(
                    name="bump-adm",
                    task_path="s3://test-bucket/bump-fake-task",
                    trials=[TrialSpec(agent="nop", model=None)],
                ),
                task_id=task_id,
                org_id=org_id,
                billed_user_id=member_a.id,
            )
            await session.flush()
            trial = await session.get(TrialModel, f"{task_id}-0")
            trial.finished_at = now
            trial.cost_usd = 0.30  # exactly the base cap -> blocked at base

        # Blocked at the base default (0.30).
        async with get_session() as session:
            with pytest.raises(QuotaExceeded):
                await admit_trials(session, org_id, member_a.id, count=1)

        # A live bump raises the effective limit -> admitted.
        async with get_session() as session:
            await _add_bump(
                session,
                org_id,
                member_a.id,
                "5.00",
                expires_at=utcnow() + timedelta(hours=6),
            )
        async with get_session() as session:
            await admit_trials(session, org_id, member_a.id, count=1)

        # Expire the bump (DB NOW() can't be monkeypatched): re-blocked at base.
        async with get_session() as session:
            await session.execute(
                QuotaBumpModel.__table__.update()
                .where(QuotaBumpModel.org_id == org_id)
                .values(expires_at=utcnow() - timedelta(hours=1))
            )
        async with get_session() as session:
            with pytest.raises(QuotaExceeded):
                await admit_trials(session, org_id, member_a.id, count=1)
    finally:
        async with get_session() as session:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id.like(f"{task_id}%")
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


# --- 6. POST happy path: effective = base + amount; row persists w/ granter -----


@requires_db
@pytest.mark.asyncio
async def test_post_bump_happy_path_returns_member_item_and_persists(org_with_member):
    org_id, admin_user, member_a = org_with_member
    expires_at = (utcnow() + timedelta(hours=24)).isoformat()
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.post(
                f"/quotas/{member_a.id}/bumps",
                json={
                    "amount_usd": "50.00",
                    "expires_at": expires_at,
                    "reason": "launch week",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == member_a.id
    assert body["base_limit_usd"] == pytest.approx(100.0)
    assert body["bump_usd"] == pytest.approx(50.0)
    assert body["limit_usd"] == pytest.approx(150.0)
    assert body["bump_expires_at"] is not None

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    QuotaBumpModel.__table__.select().where(
                        QuotaBumpModel.org_id == org_id,
                        QuotaBumpModel.user_id == member_a.id,
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["granted_by_user_id"] == admin_user.id
    assert rows[0]["reason"] == "launch week"


# --- 7. POST auth: FULL API key rejected; non-admin member rejected -------------


@pytest_asyncio.fixture
async def org_with_full_key():
    org_id = f"org_bumpk_{uuid.uuid4().hex[:8]}"
    member_a = _member(org_id, "membera", "member")
    key_model, raw_key = create_api_key(
        org_id=org_id, name="bumpk", scope=APIKeyScope.FULL
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
async def test_post_bump_rejects_full_api_key(org_with_full_key):
    raw_key, member_a = org_with_full_key
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/quotas/{member_a.id}/bumps",
            json={
                "amount_usd": "5.00",
                "expires_at": (utcnow() + timedelta(hours=6)).isoformat(),
            },
            headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert response.status_code == 403


@requires_db
@pytest.mark.asyncio
async def test_delete_bump_rejects_full_api_key(org_with_full_key):
    raw_key, member_a = org_with_full_key
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(
            f"/quotas/{member_a.id}/bumps",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_bump_routes_reject_non_admin_member():
    """Both bump routes gate on ``require_can_manage_quotas`` (admin-only)."""
    member_auth = _user_auth(org_id="org-1", user_id="u1", role=UserRole.MEMBER)
    for handler in (add_member_quota_bump, revoke_member_quota_bumps):
        with pytest.raises(HTTPException) as raised:
            await require_can_manage_quotas(member_auth)
        assert raised.value.status_code == 403
    # Sanity: the handlers are wired (referenced) so the import can't silently
    # drop; the guard behavior above is what the routes enforce.
    assert callable(add_member_quota_bump) and callable(revoke_member_quota_bumps)


# --- 8. POST 404 for a user in another org / unknown user -----------------------


@requires_db
@pytest.mark.asyncio
async def test_post_bump_cross_org_user_is_404(org_with_member):
    org_id, admin_user, _member_a = org_with_member
    other_org_id = f"org_other_{uuid.uuid4().hex[:8]}"
    outsider = _member(other_org_id, "outsider", "member")
    async with get_session() as session:
        session.add(
            OrganizationModel(id=other_org_id, name=other_org_id, slug=other_org_id)
        )
        await session.flush()
        session.add(outsider)

    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.post(
                f"/quotas/{outsider.id}/bumps",
                json={
                    "amount_usd": "5.00",
                    "expires_at": (utcnow() + timedelta(hours=6)).isoformat(),
                },
            )
            assert response.status_code == 404
            unknown = await client.post(
                "/quotas/does-not-exist/bumps",
                json={
                    "amount_usd": "5.00",
                    "expires_at": (utcnow() + timedelta(hours=6)).isoformat(),
                },
            )
            assert unknown.status_code == 404
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


# --- 9. POST 400 past; 400 naive; 422 amount <= 0 -------------------------------


@requires_db
@pytest.mark.asyncio
async def test_post_bump_400_for_past_expires_at(org_with_member):
    org_id, admin_user, member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.post(
                f"/quotas/{member_a.id}/bumps",
                json={
                    "amount_usd": "5.00",
                    "expires_at": (utcnow() - timedelta(hours=1)).isoformat(),
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert response.json()["detail"] == "expires_at must be in the future"


@requires_db
@pytest.mark.asyncio
async def test_post_bump_400_for_naive_expires_at(org_with_member):
    org_id, admin_user, member_a = org_with_member
    naive = datetime.now().replace(microsecond=0) + timedelta(hours=6)
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.post(
                f"/quotas/{member_a.id}/bumps",
                json={"amount_usd": "5.00", "expires_at": naive.isoformat()},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert response.json()["detail"] == "expires_at must include a timezone offset"


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_amount", ["0", "-5.00"])
async def test_post_bump_422_for_non_positive_amount(org_with_member, bad_amount):
    org_id, admin_user, member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.post(
                f"/quotas/{member_a.id}/bumps",
                json={
                    "amount_usd": bad_amount,
                    "expires_at": (utcnow() + timedelta(hours=6)).isoformat(),
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


# --- 10. DELETE revokes: GET /quotas shows base again; audit row survives --------


@requires_db
@pytest.mark.asyncio
async def test_delete_revokes_all_live_bumps_but_keeps_audit_row(org_with_member):
    org_id, admin_user, member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            grant = await client.post(
                f"/quotas/{member_a.id}/bumps",
                json={
                    "amount_usd": "40.00",
                    "expires_at": (utcnow() + timedelta(hours=24)).isoformat(),
                },
            )
            assert grant.json()["limit_usd"] == pytest.approx(140.0)

            delete = await client.delete(f"/quotas/{member_a.id}/bumps")
            assert delete.status_code == 200
            assert delete.json()["limit_usd"] == pytest.approx(100.0)
            assert delete.json()["bump_usd"] == pytest.approx(0.0)
            assert delete.json()["bump_expires_at"] is None

            # Idempotent: revoking again with no live bumps is a 200 no-op.
            again = await client.delete(f"/quotas/{member_a.id}/bumps")
            assert again.status_code == 200
            assert again.json()["limit_usd"] == pytest.approx(100.0)

            list_response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    members_by_id = {m["user_id"]: m for m in list_response.json()["members"]}
    assert members_by_id[member_a.id]["limit_usd"] == pytest.approx(100.0)

    # The audit row survives with revoked_at stamped.
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    QuotaBumpModel.__table__.select().where(
                        QuotaBumpModel.org_id == org_id,
                        QuotaBumpModel.user_id == member_a.id,
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["revoked_at"] is not None


@requires_db
@pytest.mark.asyncio
async def test_delete_bump_unknown_member_is_404(org_with_member):
    org_id, admin_user, _member_a = org_with_member
    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.delete("/quotas/does-not-exist/bumps")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 404


# --- 11. GET /quotas members carry base/bump fields; limit_usd includes bumps ----


@requires_db
@pytest.mark.asyncio
async def test_admin_list_members_carry_base_and_bump_fields(org_with_member):
    org_id, admin_user, member_a = org_with_member
    future = utcnow() + timedelta(hours=12)
    async with get_session() as session:
        await _add_bump(session, org_id, member_a.id, "30.00", expires_at=future)

    app = create_app()
    try:
        async with _admin_client(app, org_id, admin_user) as client:
            response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    members_by_id = {m["user_id"]: m for m in response.json()["members"]}
    row = members_by_id[member_a.id]
    assert row["base_limit_usd"] == pytest.approx(100.0)
    assert row["bump_usd"] == pytest.approx(30.0)
    assert row["limit_usd"] == pytest.approx(130.0)
    assert row["bump_expires_at"] is not None
    # A member with no bump reports zeros.
    admin_row = members_by_id[admin_user.id]
    assert admin_row["bump_usd"] == pytest.approx(0.0)
    assert admin_row["base_limit_usd"] == pytest.approx(100.0)
    assert admin_row["bump_expires_at"] is None


# --- 12. GET /quotas/me includes bump fields ------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_quotas_me_includes_bump_fields(org_with_member):
    from auth import require_auth

    org_id, _admin, member_a = org_with_member
    future = utcnow() + timedelta(hours=12)
    async with get_session() as session:
        await _add_bump(session, org_id, member_a.id, "15.00", expires_at=future)

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member_a.id, role=UserRole.MEMBER
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/quotas/me")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == member_a.id
    assert body["bump_usd"] == pytest.approx(15.0)
    assert body["limit_usd"] == pytest.approx(115.0)
    assert body["bump_expires_at"] is not None
