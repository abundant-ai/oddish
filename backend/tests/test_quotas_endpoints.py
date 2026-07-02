"""S3 usage-visibility endpoints: GET /quotas/me + admin GET /quotas.

Auth-dependency unit tests run without a DB; the content + rejection tests hit
the real ASGI app. User-auth (Clerk) cases use FastAPI dependency_overrides so we
do not need to mint real Clerk tokens; the FULL-API-key rejection case uses a
real key to prove GET /quotas rejects API keys (unlike require_admin).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import can_manage_quotas, require_auth, require_can_manage_quotas
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, OrganizationModel, UserModel, UserRole
from oddish.core.api_keys import create_api_key
from oddish.db import TrialModel, WorkerJobModel, get_session
from oddish.queue import create_task
from oddish.schemas import TaskSubmission, TrialSpec

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _user_auth(*, org_id: str, user_id: str, role: UserRole) -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=org_id, user_id=user_id, user_role=role
    )


# --- S3-T5: the admin dependency is user-auth-only ADMIN (no FULL-key bypass) --


@pytest.mark.asyncio
async def test_require_can_manage_quotas_rejects_api_key():
    api_key_auth = AuthContext(method=AuthMethod.API_KEY, org_id="org-1")
    with pytest.raises(HTTPException) as raised:
        await require_can_manage_quotas(api_key_auth)
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_require_can_manage_quotas_rejects_non_admin_member():
    member_auth = _user_auth(org_id="org-1", user_id="u1", role=UserRole.MEMBER)
    with pytest.raises(HTTPException) as raised:
        await require_can_manage_quotas(member_auth)
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_require_can_manage_quotas_allows_admin_user():
    admin_auth = _user_auth(org_id="org-1", user_id="u1", role=UserRole.ADMIN)
    assert await require_can_manage_quotas(admin_auth) is admin_auth


def test_can_manage_quotas_is_admin_only():
    assert can_manage_quotas(_user_auth(org_id="o", user_id="u", role=UserRole.ADMIN))
    assert not can_manage_quotas(
        _user_auth(org_id="o", user_id="u", role=UserRole.MEMBER)
    )


# --- shared fixture: org + admin + two members; member_a has today's spend ------


def _submission(name: str, *, n_trials: int) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/quota-endpoint-fake-task",
        trials=[TrialSpec(agent="nop", model=None) for _ in range(n_trials)],
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


@pytest_asyncio.fixture
async def org_with_spend():
    org_id = f"org_quota_{uuid.uuid4().hex[:8]}"
    admin_user = _member(org_id, "admin", "admin")
    member_a = _member(org_id, "membera", "member")
    member_b = _member(org_id, "memberb", "member")
    task_id = f"task_quota_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add_all([admin_user, member_a, member_b])
        await session.flush()
        await create_task(
            session,
            _submission("quota-endpoint", n_trials=2),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=member_a.id,
        )
        await session.flush()
        first_trial = await session.get(TrialModel, f"{task_id}-0")
        first_trial.finished_at = now
        first_trial.cost_usd = 0.10
        second_trial = await session.get(TrialModel, f"{task_id}-1")
        second_trial.finished_at = now
        second_trial.cost_usd = 0.20

    try:
        yield org_id, admin_user, member_a, member_b
    finally:
        async with get_session() as session:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id.like(f"{task_id}%")
                )
            )
            from oddish.db import TaskModel

            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.id.in_([admin_user.id, member_a.id, member_b.id])
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


# --- S3-T6: admin list shows the default limit + grouped usage, 0 for no-trials -


@requires_db
@pytest.mark.asyncio
async def test_admin_quota_list_shows_grouped_usage_and_default_limit(org_with_spend):
    org_id, admin_user, member_a, member_b = org_with_spend
    app = create_app()
    app.dependency_overrides[require_can_manage_quotas] = lambda: _user_auth(
        org_id=org_id, user_id=admin_user.id, role=UserRole.ADMIN
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/quotas")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    members_by_id = {m["user_id"]: m for m in response.json()["members"]}

    assert members_by_id[member_a.id]["used_usd"] == pytest.approx(0.30)
    assert members_by_id[member_b.id]["used_usd"] == pytest.approx(0.0)
    assert members_by_id[member_a.id]["limit_usd"] == pytest.approx(10.0)


# --- S3-T4: /quotas/me returns ONLY the caller's usage --------------------------


@requires_db
@pytest.mark.asyncio
async def test_quotas_me_returns_only_callers_usage(org_with_spend):
    org_id, admin_user, member_a, member_b = org_with_spend
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
    assert body["used_usd"] == pytest.approx(0.30)
    assert body["limit_usd"] == pytest.approx(10.0)


# --- S3-T5 (end to end): a FULL-scope API key is rejected by GET /quotas ---------


@pytest_asyncio.fixture
async def org_with_full_key():
    org_id = f"org_qk_{uuid.uuid4().hex[:8]}"
    key_model, raw_key = create_api_key(
        org_id=org_id, name="qk", scope=APIKeyScope.FULL
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
async def test_admin_quota_list_rejects_full_api_key(org_with_full_key):
    raw_key = org_with_full_key
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/quotas", headers={"Authorization": f"Bearer {raw_key}"}
        )
    assert response.status_code == 403
