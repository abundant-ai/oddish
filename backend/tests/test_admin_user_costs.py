"""Admin per-user cost drilldown: GET /admin/costs/users/{user_id}.

DB-backed (requires_db). Trials are inserted directly so each scenario controls
finished_at / cost_usd / billed_user_id / tokens / model / deleted_at exactly.
require_admin is exercised for real (403) and overridden for the content cases.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import require_admin, require_auth
from auth.types import AuthContext, AuthMethod
from models import OrganizationModel, UserModel, UserRole
from oddish.db import ExperimentModel, TaskModel, TrialModel, get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")

# claude-haiku-4: input=1e-6, output=5e-6. 1000 in + 500 out (no cache) =>
# 0.001 + 0.0025 = 0.0035 estimated when cost_usd is NULL.
_EST_MODEL = "claude-haiku-4"
_EST_EXPECTED = 0.0035


def _admin_auth(org_id: str, user_id: str) -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=org_id,
        user_id=user_id,
        user_role=UserRole.ADMIN,
    )


def _member(org_id: str, handle: str, role: str = "member") -> UserModel:
    suffix = uuid.uuid4().hex[:8]
    return UserModel(
        id=f"user_{handle}_{suffix}",
        org_id=org_id,
        email=f"{handle}_{suffix}@example.com",
        name=handle,
        github_username=handle,
        clerk_user_id=f"clerk_{suffix}",
        role=role,
        is_active=True,
    )


class _Fixture:
    def __init__(self, org_id, admin, target, other, task_ids):
        self.org_id = org_id
        self.admin = admin
        self.target = target
        self.other = other
        self.task_ids = task_ids


def _trial(
    task_id: str,
    idx: int,
    *,
    org_id: str,
    billed_user_id: str | None,
    finished_at: datetime | None,
    cost_usd: float | None,
    model: str | None = _EST_MODEL,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    deleted_at: datetime | None = None,
) -> TrialModel:
    return TrialModel(
        id=f"{task_id}-{idx}",
        name=f"trial-{idx}",
        task_id=task_id,
        experiment_id=f"exp_{task_id}",
        org_id=org_id,
        billed_user_id=billed_user_id,
        agent="nop",
        provider="anthropic",
        queue_key="test",
        model=model,
        status="success",
        finished_at=finished_at,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        deleted_at=deleted_at,
    )


@pytest_asyncio.fixture
async def costs_fixture():
    org_id = f"org_uc_{uuid.uuid4().hex[:8]}"
    admin = _member(org_id, "admin", "admin")
    target = _member(org_id, "target")
    other = _member(org_id, "other")
    task_a = f"task_a_{uuid.uuid4().hex[:8]}"
    task_b = f"task_b_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    recent = now - timedelta(days=1)
    old = now - timedelta(days=40)

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add_all([admin, target, other])
        await session.flush()
        session.add_all(
            [
                ExperimentModel(id=f"exp_{task_a}", name="exp-a", org_id=org_id),
                ExperimentModel(id=f"exp_{task_b}", name="exp-b", org_id=org_id),
            ]
        )
        session.add(
            TaskModel(
                id=task_a, name="alpha-task", org_id=org_id, user="x",
                task_path="s3://test-bucket/alpha",
            )
        )
        session.add(
            TaskModel(
                id=task_b, name="beta-task", org_id=org_id, user="x",
                task_path="s3://test-bucket/beta",
            )
        )
        await session.flush()
        session.add_all(
            [
                # task_a: two native-cost trials for the target, recent window.
                _trial(
                    task_a, 0, org_id=org_id, billed_user_id=target.id,
                    finished_at=recent, cost_usd=0.10,
                ),
                _trial(
                    task_a, 1, org_id=org_id, billed_user_id=target.id,
                    finished_at=recent, cost_usd=0.20,
                ),
                # task_b: one estimate-only trial (cost_usd NULL) for the target.
                _trial(
                    task_b, 0, org_id=org_id, billed_user_id=target.id,
                    finished_at=recent, cost_usd=None,
                    input_tokens=1000, output_tokens=500,
                ),
                # Another user's billed trial -- must be excluded.
                _trial(
                    task_a, 2, org_id=org_id, billed_user_id=other.id,
                    finished_at=recent, cost_usd=5.00,
                ),
                # NULL billed_user_id -- draws down nobody, must be excluded.
                _trial(
                    task_a, 3, org_id=org_id, billed_user_id=None,
                    finished_at=recent, cost_usd=7.00,
                ),
                # target, but never settled (finished_at NULL) -- excluded always.
                _trial(
                    task_a, 4, org_id=org_id, billed_user_id=target.id,
                    finished_at=None, cost_usd=9.00,
                ),
                # target, settled but 40 days ago -- outside a 7d window.
                _trial(
                    task_a, 5, org_id=org_id, billed_user_id=target.id,
                    finished_at=old, cost_usd=3.00,
                ),
                # target, soft-deleted but settled and recent -- INCLUDED.
                _trial(
                    task_b, 1, org_id=org_id, billed_user_id=target.id,
                    finished_at=recent, cost_usd=0.05, deleted_at=now,
                ),
            ]
        )

    fixture = _Fixture(org_id, admin, target, other, (task_a, task_b))
    try:
        yield fixture
    finally:
        async with get_session() as session:
            for tid in (task_a, task_b):
                await session.execute(
                    TrialModel.__table__.delete().where(
                        TrialModel.task_id == tid
                    )
                )
                await session.execute(
                    TaskModel.__table__.delete().where(TaskModel.id == tid)
                )
                await session.execute(
                    ExperimentModel.__table__.delete().where(
                        ExperimentModel.id == f"exp_{tid}"
                    )
                )
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.id.in_([admin.id, target.id, other.id])
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


async def _get(
    org_id: str, admin_id: str, user_id: str, *, params: dict | None = None
):
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: _admin_auth(org_id, admin_id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(
                f"/admin/costs/users/{user_id}", params=params or {}
            )
    finally:
        app.dependency_overrides.clear()


@requires_db
@pytest.mark.asyncio
async def test_attribution_settled_soft_delete_and_window(costs_fixture):
    f = costs_fixture
    resp = await _get(
        f.org_id, f.admin.id, f.target.id, params={"window_days": 7}
    )
    assert resp.status_code == 200
    body = resp.json()

    # target identity is filled from the UserModel row.
    assert body["billed_user_id"] == f.target.id
    assert body["org_id"] == f.org_id
    assert body["name"] == f.target.name
    assert body["email"] == f.target.email
    assert body["github_username"] == f.target.github_username

    totals = body["totals"]
    # Included, within 7d, settled, billed to target:
    #   task_a: 0.10 + 0.20 native
    #   task_b: 0.0035 estimate + 0.05 native (soft-deleted, still counted)
    # Excluded: other user (5.00), NULL billed (7.00), unsettled (9.00),
    #   40d-old (3.00).
    assert totals["trial_count"] == 4
    assert totals["task_count"] == 2
    assert totals["cost_native_usd"] == pytest.approx(0.35)
    assert totals["cost_estimated_usd"] == pytest.approx(_EST_EXPECTED)
    assert totals["cost_usd"] == pytest.approx(0.35 + _EST_EXPECTED)
    assert totals["window_days"] == 7


@requires_db
@pytest.mark.asyncio
async def test_task_grouping_and_sort(costs_fixture):
    f = costs_fixture
    resp = await _get(f.org_id, f.admin.id, f.target.id, params={"window_days": 7})
    body = resp.json()
    tasks = body["tasks"]
    assert len(tasks) == 2
    # task_a (0.30) sorts before task_b (~0.0535).
    assert tasks[0]["task_name"] == "alpha-task"
    assert tasks[0]["cost_usd"] == pytest.approx(0.30)
    assert tasks[0]["trial_count"] == 2
    assert tasks[0]["cost_estimated_usd"] == pytest.approx(0.0)
    assert tasks[1]["task_name"] == "beta-task"
    assert tasks[1]["cost_usd"] == pytest.approx(0.05 + _EST_EXPECTED)
    assert tasks[1]["trial_count"] == 2
    assert tasks[1]["cost_estimated_usd"] == pytest.approx(_EST_EXPECTED)
    # per-task model split is present.
    assert tasks[1]["models"][0]["model"] == _EST_MODEL


@requires_db
@pytest.mark.asyncio
async def test_series_by_model_shape(costs_fixture):
    f = costs_fixture
    resp = await _get(f.org_id, f.admin.id, f.target.id, params={"window_days": 7})
    body = resp.json()
    series = body["series_by_model"]
    assert series["dimension"] == "model"
    assert any(k["key"] == _EST_MODEL for k in series["keys"])
    assert series["buckets"], "expected at least one time bucket"
    for bucket in series["buckets"]:
        assert "bucket_start" in bucket
        assert "cost_usd" in bucket
        assert isinstance(bucket["costs"], dict)


@requires_db
@pytest.mark.asyncio
async def test_all_time_still_excludes_unsettled(costs_fixture):
    f = costs_fixture
    resp = await _get(f.org_id, f.admin.id, f.target.id, params={"window_days": 0})
    body = resp.json()
    totals = body["totals"]
    assert totals["window_days"] is None
    # All-time pulls in the 40d-old 3.00 trial too, but NOT the unsettled 9.00.
    assert totals["trial_count"] == 5
    assert totals["cost_native_usd"] == pytest.approx(0.35 + 3.00)
    assert totals["cost_estimated_usd"] == pytest.approx(_EST_EXPECTED)


@requires_db
@pytest.mark.asyncio
async def test_task_limit_cap(costs_fixture):
    f = costs_fixture
    resp = await _get(
        f.org_id, f.admin.id, f.target.id, params={"window_days": 0, "task_limit": 1}
    )
    body = resp.json()
    # Two tasks in the data; capped to the top 1, but task_count reports the full set.
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["task_name"] == "alpha-task"
    assert body["totals"]["task_count"] == 2


@requires_db
@pytest.mark.asyncio
async def test_zero_trial_user_returns_empty_200(costs_fixture):
    f = costs_fixture
    # ``other`` is billed on exactly one excluded-from-target trial; query for a
    # brand-new user with no trials at all.
    fresh = _member(f.org_id, "fresh")
    async with get_session() as session:
        session.add(fresh)
    try:
        resp = await _get(f.org_id, f.admin.id, fresh.id, params={"window_days": 7})
        assert resp.status_code == 200
        body = resp.json()
        assert body["billed_user_id"] == fresh.id
        assert body["totals"]["trial_count"] == 0
        assert body["totals"]["task_count"] == 0
        assert body["totals"]["cost_usd"] == pytest.approx(0.0)
        assert body["tasks"] == []
        assert body["series_by_model"]["buckets"] == []
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == fresh.id)
            )


@requires_db
@pytest.mark.asyncio
async def test_soft_deleted_user_still_resolvable(costs_fixture):
    """Offboarded (soft-deleted) users keep historical billed trials and the
    Costs tab still links them, so the drilldown must not 404 on them."""
    f = costs_fixture
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        await session.execute(
            UserModel.__table__.update()
            .where(UserModel.id == f.target.id)
            .values(deleted_at=now)
        )
    try:
        resp = await _get(f.org_id, f.admin.id, f.target.id, params={"window_days": 7})
        assert resp.status_code == 200
        body = resp.json()
        assert body["billed_user_id"] == f.target.id
        assert body["name"] == f.target.name
        assert body["totals"]["trial_count"] == 4
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.update()
                .where(UserModel.id == f.target.id)
                .values(deleted_at=None)
            )


@requires_db
@pytest.mark.asyncio
async def test_unknown_user_returns_404(costs_fixture):
    f = costs_fixture
    resp = await _get(f.org_id, f.admin.id, "user_does_not_exist_zzz")
    assert resp.status_code == 404


@requires_db
@pytest.mark.asyncio
async def test_non_admin_returns_403(costs_fixture):
    f = costs_fixture
    # Override the underlying auth dependency with a MEMBER context so the real
    # require_admin runs and rejects (proves the role gate, not a stubbed 403).
    member_auth = AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=f.org_id,
        user_id=f.target.id,
        user_role=UserRole.MEMBER,
    )
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: member_auth
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/admin/costs/users/{f.target.id}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
