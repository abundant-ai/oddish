from __future__ import annotations

import os
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import ProgrammingError

from api.app import create_app
from auth import require_auth, require_can_manage_quotas
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
from oddish.db.models import APIKeyModel
from oddish.queue import create_task
from oddish.schemas import TaskSubmission, TrialSpec

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")

# Base limit these tests bank on. Derive it from the setting so the assertions
# don't rot when the workspace default changes (it moved 100 -> 200 in #608).
BASE = float(settings.default_daily_quota_usd)


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


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _bump_payload(amount="5.00", hours=6, **extra):
    return {"amount_usd": amount, "duration_hours": hours, **extra}


async def _add_bump(
    session, org_id, user_id, amount, *, expires_at, revoked_at=None, deleted_at=None
):
    session.add(
        QuotaBumpModel(
            id=uuid.uuid4().hex[:8],
            org_id=org_id,
            user_id=user_id,
            amount_usd=Decimal(str(amount)),
            expires_at=expires_at,
            revoked_at=revoked_at,
            deleted_at=deleted_at,
        )
    )
    await session.flush()


async def _bump_rows(org_id, user_id):
    async with get_session() as session:
        result = await session.execute(
            QuotaBumpModel.__table__.select().where(
                QuotaBumpModel.org_id == org_id,
                QuotaBumpModel.user_id == user_id,
            )
        )
        return result.mappings().all()


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
    assert "down_revision" in source
    assert '"taskapikey_be_001"' in source

    uppercased = source.upper()
    assert "INSERT INTO" not in uppercased
    assert "UPDATE QUOTA_BUMPS" not in uppercased


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


@pytest_asyncio.fixture
async def admin_client(org_with_member):
    org_id, admin_user, member_a = org_with_member
    app = create_app()
    app.dependency_overrides[require_can_manage_quotas] = lambda: _user_auth(
        org_id=org_id, user_id=admin_user.id, role=UserRole.ADMIN
    )
    async with _client(app) as client:
        yield client, org_id, admin_user, member_a


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, "5.00"])
async def test_effective_limit_is_base_plus_live_bump(org_with_member, override):
    org_id, _admin, member_a = org_with_member
    async with get_session() as session:
        if override:
            session.add(
                QuotaModel(
                    org_id=org_id, user_id=member_a.id, limit_usd=Decimal(override)
                )
            )
            await session.flush()
        await _add_bump(
            session,
            org_id,
            member_a.id,
            "10.00",
            expires_at=utcnow() + timedelta(hours=6),
        )
        effective = await get_effective_limit(session, org_id, member_a.id)
    base = Decimal(override) if override else settings.default_daily_quota_usd
    assert effective == base + Decimal("10.0000")


@requires_db
@pytest.mark.asyncio
async def test_live_bump_total_excludes_expired_revoked_tombstoned_and_sums(
    org_with_member,
):
    org_id, _admin, member_a = org_with_member
    past = utcnow() - timedelta(hours=1)
    future = utcnow() + timedelta(hours=6)
    later = utcnow() + timedelta(hours=48)
    async with get_session() as session:
        await _add_bump(session, org_id, member_a.id, "100.00", expires_at=past)
        await _add_bump(
            session,
            org_id,
            member_a.id,
            "200.00",
            expires_at=future,
            revoked_at=utcnow(),
        )
        await _add_bump(
            session,
            org_id,
            member_a.id,
            "400.00",
            expires_at=future,
            deleted_at=utcnow(),
        )
        await _add_bump(session, org_id, member_a.id, "7.00", expires_at=future)
        await _add_bump(session, org_id, member_a.id, "3.00", expires_at=later)

        total, max_expires = await live_bump_total(session, org_id, member_a.id)

    assert total == Decimal("10.0000")
    assert max_expires is not None
    assert abs((max_expires - later).total_seconds()) < 1


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
            trial.finished_at = utcnow()
            trial.cost_usd = 0.30

        async with get_session() as session:
            with pytest.raises(QuotaExceeded):
                await admit_trials(session, org_id, member_a.id, count=1)

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


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "expected_bump"),
    [("50.00", 50.0), ("0.01", 0.01), ("99999999.9999", 99999999.9999)],
)
async def test_post_bump_happy_path_returns_member_item_and_persists(
    admin_client, amount, expected_bump
):
    client, org_id, admin_user, member_a = admin_client
    response = await client.post(
        f"/quotas/{member_a.id}/bumps",
        json=_bump_payload(amount, hours=24, reason="launch week"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == member_a.id
    assert body["base_limit_usd"] == pytest.approx(BASE)
    assert body["bump_usd"] == pytest.approx(expected_bump)
    assert body["limit_usd"] == pytest.approx(BASE + expected_bump)
    assert body["bump_expires_at"] is not None

    rows = await _bump_rows(org_id, member_a.id)
    assert len(rows) == 1
    assert rows[0]["granted_by_user_id"] == admin_user.id
    assert rows[0]["reason"] == "launch week"


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
@pytest.mark.parametrize("method", ["post", "delete"])
async def test_bump_routes_reject_full_api_key(org_with_full_key, method):
    raw_key, member_a = org_with_full_key
    kwargs = {"json": _bump_payload()} if method == "post" else {}
    async with _client(create_app()) as client:
        response = await getattr(client, method)(
            f"/quotas/{member_a.id}/bumps",
            headers={"Authorization": f"Bearer {raw_key}"},
            **kwargs,
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_member_403s_via_real_can_manage_quotas_dependency():
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id="org-1", user_id="u1", role=UserRole.MEMBER
    )
    async with _client(app) as client:
        post = await client.post("/quotas/u2/bumps", json=_bump_payload())
        delete = await client.delete("/quotas/u2/bumps")
    assert post.status_code == 403
    assert delete.status_code == 403


@requires_db
@pytest.mark.asyncio
async def test_post_bump_cross_org_user_is_404(admin_client):
    client, _org_id, _admin, _member_a = admin_client
    other_org_id = f"org_other_{uuid.uuid4().hex[:8]}"
    outsider = _member(other_org_id, "outsider", "member")
    async with get_session() as session:
        session.add(
            OrganizationModel(id=other_org_id, name=other_org_id, slug=other_org_id)
        )
        await session.flush()
        session.add(outsider)

    try:
        for target in (outsider.id, "does-not-exist"):
            response = await client.post(
                f"/quotas/{target}/bumps", json=_bump_payload()
            )
            assert response.status_code == 404
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == outsider.id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == other_org_id
                )
            )


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "hours"),
    [
        ("0", 6),
        ("-5.00", 6),
        ("5.00001", 6),
        ("100000000.00", 6),
        ("5.00", 0),
        ("5.00", -6),
        ("5.00", 8761),
    ],
)
async def test_post_bump_rejects_invalid_payloads(admin_client, amount, hours):
    client, _org_id, _admin, member_a = admin_client
    response = await client.post(
        f"/quotas/{member_a.id}/bumps", json=_bump_payload(amount, hours=hours)
    )
    assert response.status_code == 422


@requires_db
@pytest.mark.asyncio
async def test_bump_lifecycle_revoke_keeps_audit_and_regrant_counts_only_live(
    admin_client,
):
    client, org_id, _admin, member_a = admin_client
    grant = await client.post(
        f"/quotas/{member_a.id}/bumps", json=_bump_payload("40.00", hours=24)
    )
    assert grant.json()["limit_usd"] == pytest.approx(BASE + 40.0)

    delete = await client.delete(f"/quotas/{member_a.id}/bumps")
    assert delete.status_code == 200
    assert delete.json()["limit_usd"] == pytest.approx(BASE)
    assert delete.json()["bump_usd"] == pytest.approx(0.0)
    assert delete.json()["bump_expires_at"] is None

    again = await client.delete(f"/quotas/{member_a.id}/bumps")
    assert again.status_code == 200
    assert again.json()["limit_usd"] == pytest.approx(BASE)

    regrant = await client.post(
        f"/quotas/{member_a.id}/bumps", json=_bump_payload("25.00", hours=12)
    )
    assert regrant.json()["bump_usd"] == pytest.approx(25.0)
    assert regrant.json()["limit_usd"] == pytest.approx(BASE + 25.0)

    list_response = await client.get("/quotas")
    members_by_id = {m["user_id"]: m for m in list_response.json()["members"]}
    assert members_by_id[member_a.id]["limit_usd"] == pytest.approx(BASE + 25.0)

    rows = await _bump_rows(org_id, member_a.id)
    assert len(rows) == 2
    by_amount = {row["amount_usd"]: row for row in rows}
    assert by_amount[Decimal("40.0000")]["revoked_at"] is not None
    assert by_amount[Decimal("25.0000")]["revoked_at"] is None


@requires_db
@pytest.mark.asyncio
async def test_delete_bump_unknown_member_is_404(admin_client):
    client, _org_id, _admin, _member_a = admin_client
    response = await client.delete("/quotas/does-not-exist/bumps")
    assert response.status_code == 404


@requires_db
@pytest.mark.asyncio
async def test_admin_list_members_carry_base_and_bump_fields(admin_client):
    client, org_id, admin_user, member_a = admin_client
    async with get_session() as session:
        await _add_bump(
            session,
            org_id,
            member_a.id,
            "30.00",
            expires_at=utcnow() + timedelta(hours=12),
        )

    response = await client.get("/quotas")

    assert response.status_code == 200
    members_by_id = {m["user_id"]: m for m in response.json()["members"]}
    row = members_by_id[member_a.id]
    assert row["base_limit_usd"] == pytest.approx(BASE)
    assert row["bump_usd"] == pytest.approx(30.0)
    assert row["limit_usd"] == pytest.approx(BASE + 30.0)
    assert row["bump_expires_at"] is not None
    admin_row = members_by_id[admin_user.id]
    assert admin_row["bump_usd"] == pytest.approx(0.0)
    assert admin_row["base_limit_usd"] == pytest.approx(BASE)
    assert admin_row["bump_expires_at"] is None


@requires_db
@pytest.mark.asyncio
async def test_quotas_me_includes_bump_fields(org_with_member):
    org_id, _admin, member_a = org_with_member
    async with get_session() as session:
        await _add_bump(
            session,
            org_id,
            member_a.id,
            "15.00",
            expires_at=utcnow() + timedelta(hours=12),
        )

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member_a.id, role=UserRole.MEMBER
    )
    async with _client(app) as client:
        response = await client.get("/quotas/me")

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == member_a.id
    assert body["base_limit_usd"] == pytest.approx(BASE)
    assert body["bump_usd"] == pytest.approx(15.0)
    assert body["limit_usd"] == pytest.approx(BASE + 15.0)
    assert body["bump_expires_at"] is not None


def _undefined_quota_bumps_error() -> ProgrammingError:
    class _UndefinedTableError(Exception):
        sqlstate = "42P01"
        pgcode = "42P01"

    return ProgrammingError(
        'SELECT ... FROM quota_bumps',
        {},
        _UndefinedTableError('relation "quota_bumps" does not exist'),
    )


@requires_db
@pytest.mark.asyncio
async def test_list_quotas_degrades_when_bump_table_missing(admin_client, monkeypatch):
    # Deploy-before-migrate: GET /quotas must not 500 when quota_bumps is absent;
    # members simply read as un-boosted (limit == base).
    client, _org_id, _admin, member_a = admin_client
    import api.routers.orgs as orgs_mod

    async def _boom(*_a, **_k):
        raise _undefined_quota_bumps_error()

    monkeypatch.setattr(orgs_mod, "live_bump_totals_by_user", _boom)

    response = await client.get("/quotas")

    assert response.status_code == 200
    row = {m["user_id"]: m for m in response.json()["members"]}[member_a.id]
    assert row["bump_usd"] == pytest.approx(0.0)
    assert row["bump_expires_at"] is None
    assert row["limit_usd"] == pytest.approx(row["base_limit_usd"])


@requires_db
@pytest.mark.asyncio
async def test_me_degrades_when_bump_table_missing(org_with_member, monkeypatch):
    # The savepoint must roll back the failed bump read and leave the request's
    # transaction usable, so the reserved-usd read after it still succeeds.
    org_id, _admin, member_a = org_with_member
    import api.routers.orgs as orgs_mod

    async def _boom(*_a, **_k):
        raise _undefined_quota_bumps_error()

    monkeypatch.setattr(orgs_mod, "live_bump_total", _boom)

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=member_a.id, role=UserRole.MEMBER
    )
    async with _client(app) as client:
        response = await client.get("/quotas/me")

    assert response.status_code == 200
    body = response.json()
    assert body["bump_usd"] == pytest.approx(0.0)
    assert body["bump_expires_at"] is None
    assert body["limit_usd"] == pytest.approx(body["base_limit_usd"])
