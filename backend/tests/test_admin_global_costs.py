"""DB-backed tests for get_cost_breakdown_core (global /admin/costs breakdown)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from models import OrganizationModel, OrgQuotaModel, QuotaModel, UserModel
from oddish.config import settings
from oddish.core.admin import get_cost_breakdown_core, get_cost_leaderboard_core
from oddish.db import ExperimentModel, TaskModel, TrialModel, get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


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


def _trial(
    task_id: str,
    idx: int,
    *,
    org_id: str,
    billed_user_id: str | None,
    finished_at: datetime | None,
    cost_usd: float | None,
    created_at: datetime,
    status: str = "success",
    started_at: datetime | None = None,
    model: str | None = "claude-haiku-4",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
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
        status=status,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class _Fixture:
    def __init__(self, org_id, target, other, task_id, baseline, recent, prior):
        self.org_id = org_id
        self.target = target
        self.other = other
        self.task_id = task_id
        self.baseline = baseline
        self.recent = recent
        self.prior = prior


@pytest_asyncio.fixture
async def global_costs_fixture():
    org_id = f"org_gc_{uuid.uuid4().hex[:8]}"
    target = _member(org_id, "target")
    other = _member(org_id, "other")
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    prior = now - timedelta(days=10)

    async with get_session() as session:
        baseline = (await get_cost_breakdown_core(session, window_days=7)).totals

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add_all([target, other])
        await session.flush()
        session.add(ExperimentModel(id=f"exp_{task_id}", name="exp", org_id=org_id))
        session.add(
            TaskModel(
                id=task_id, name="task", org_id=org_id, user="x",
                task_path="s3://test-bucket/t",
            )
        )
        await session.flush()
        session.add_all(
            [
                _trial(
                    task_id, 0, org_id=org_id, billed_user_id=target.id,
                    created_at=recent, finished_at=recent, cost_usd=1.00,
                ),
                _trial(
                    task_id, 1, org_id=org_id, billed_user_id=target.id,
                    created_at=recent, finished_at=recent, cost_usd=0.50,
                    status="failed",
                ),
                _trial(
                    task_id, 2, org_id=org_id, billed_user_id=other.id,
                    created_at=recent, finished_at=recent, cost_usd=2.00,
                ),
                _trial(
                    task_id, 3, org_id=org_id, billed_user_id=target.id,
                    created_at=prior, finished_at=prior, cost_usd=4.00,
                ),
                _trial(
                    task_id, 4, org_id=org_id, billed_user_id=target.id,
                    created_at=prior, finished_at=prior, cost_usd=0.25,
                    status="failed",
                ),
                _trial(
                    task_id, 5, org_id=org_id, billed_user_id=target.id,
                    created_at=recent, finished_at=None, cost_usd=None,
                    status="running",
                ),
            ]
        )

    fixture = _Fixture(org_id, target, other, task_id, baseline, recent, prior)
    try:
        yield fixture
    finally:
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id == f"exp_{task_id}"
                )
            )
            await session.execute(
                QuotaModel.__table__.delete().where(QuotaModel.org_id == org_id)
            )
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.id.in_([target.id, other.id])
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


def _user_row(result, user_id, org_id=None):
    return next(
        u
        for u in result.by_user
        if u.owner_user_id == user_id and (org_id is None or u.org_id == org_id)
    )


@requires_db
@pytest.mark.asyncio
async def test_window_costs_by_user(global_costs_fixture):
    f = global_costs_fixture
    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=7)
    row = _user_row(result, f.target.id)
    assert row.cost_usd == pytest.approx(1.50)
    assert row.trial_count == 3


@requires_db
@pytest.mark.asyncio
async def test_cost_leaderboard_uses_same_settled_spend(global_costs_fixture):
    f = global_costs_fixture
    async with get_session() as session:
        leaders = await get_cost_leaderboard_core(session, window_days=7)
    by_user = {leader.user_id: leader.cost_usd for leader in leaders}
    assert by_user[f.target.id] == pytest.approx(1.50)
    assert by_user[f.other.id] == pytest.approx(2.00)


@requires_db
@pytest.mark.asyncio
async def test_prev_window_totals_and_per_user(global_costs_fixture):
    f = global_costs_fixture
    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=7)
    assert result.totals.prev_cost_usd - f.baseline.prev_cost_usd == pytest.approx(4.25)
    row = _user_row(result, f.target.id)
    assert row.prev_cost_usd == pytest.approx(4.25)
    other_row = _user_row(result, f.other.id)
    assert other_row.prev_cost_usd == pytest.approx(0.0)


@requires_db
@pytest.mark.asyncio
async def test_month_to_date_cost(global_costs_fixture):
    f = global_costs_fixture
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=7)
    expected = (3.50 if f.recent >= month_start else 0.0) + (
        4.25 if f.prior >= month_start else 0.0
    )
    assert result.totals.month_cost_usd - f.baseline.month_cost_usd == pytest.approx(
        expected
    )


@requires_db
@pytest.mark.asyncio
async def test_month_budget_sums_org_overrides(global_costs_fixture):
    f = global_costs_fixture
    async with get_session() as session:
        session.add(OrgQuotaModel(org_id=f.org_id, limit_usd=500))
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=7)
    org_has_month_spend = f.recent >= month_start or f.prior >= month_start
    expected = 500.0 if org_has_month_spend else 0.0
    assert (result.totals.month_budget_usd or 0.0) - (
        f.baseline.month_budget_usd or 0.0
    ) == pytest.approx(expected)


@requires_db
@pytest.mark.asyncio
async def test_prev_fields_none_all_time(global_costs_fixture):
    f = global_costs_fixture
    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=None)
    assert result.totals.prev_cost_usd is None
    row = _user_row(result, f.target.id)
    assert row.prev_cost_usd is None


@requires_db
@pytest.mark.asyncio
async def test_inflight_counts(global_costs_fixture):
    f = global_costs_fixture
    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=7)
    row = _user_row(result, f.target.id)
    assert row.inflight_trial_count == 1
    other_row = _user_row(result, f.other.id)
    assert other_row.inflight_trial_count == 0


@requires_db
@pytest.mark.asyncio
async def test_quota_spent_and_limit(global_costs_fixture):
    f = global_costs_fixture
    async with get_session() as session:
        session.add(
            QuotaModel(org_id=f.org_id, user_id=f.target.id, limit_usd=42)
        )
    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=7)
    row = _user_row(result, f.target.id)
    assert row.quota_limit_usd == pytest.approx(42.0)
    assert row.quota_spent_usd == pytest.approx(1.50)
    other_row = _user_row(result, f.other.id)
    assert other_row.quota_limit_usd is not None
    assert other_row.quota_spent_usd == pytest.approx(2.00)


@requires_db
@pytest.mark.asyncio
async def test_quota_limit_uses_same_org_as_enforcement(global_costs_fixture):
    f = global_costs_fixture
    foreign_org_id = f"org_gc_foreign_{uuid.uuid4().hex[:8]}"
    foreign_task_id = f"task_foreign_{uuid.uuid4().hex[:8]}"
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        async with get_session() as session:
            session.add(
                OrganizationModel(
                    id=foreign_org_id, name=foreign_org_id, slug=foreign_org_id
                )
            )
            await session.flush()
            session.add(
                QuotaModel(org_id=foreign_org_id, user_id=f.target.id, limit_usd=99)
            )
            session.add(
                ExperimentModel(
                    id=f"exp_{foreign_task_id}",
                    name="foreign exp",
                    org_id=foreign_org_id,
                )
            )
            session.add(
                TaskModel(
                    id=foreign_task_id,
                    name="foreign task",
                    org_id=foreign_org_id,
                    user="x",
                    task_path="s3://test-bucket/foreign",
                )
            )
            await session.flush()
            session.add_all(
                [
                    _trial(
                        foreign_task_id,
                        0,
                        org_id=foreign_org_id,
                        billed_user_id=f.target.id,
                        created_at=recent,
                        finished_at=recent,
                        cost_usd=7,
                    ),
                    _trial(
                        foreign_task_id,
                        1,
                        org_id=foreign_org_id,
                        billed_user_id=f.target.id,
                        created_at=recent,
                        finished_at=None,
                        cost_usd=None,
                        status="running",
                    ),
                ]
            )

        async with get_session() as session:
            result = await get_cost_breakdown_core(session, window_days=7)

        home_row = _user_row(result, f.target.id, org_id=f.org_id)
        assert home_row.cost_usd == pytest.approx(1.50)
        assert home_row.quota_spent_usd == pytest.approx(1.50)
        assert home_row.quota_limit_usd == pytest.approx(
            float(settings.default_daily_quota_usd)
        )
        assert home_row.inflight_trial_count == 1

        foreign_row = _user_row(result, f.target.id, org_id=foreign_org_id)
        assert foreign_row.cost_usd == pytest.approx(7.00)
        assert foreign_row.quota_spent_usd == pytest.approx(7.00)
        assert foreign_row.quota_limit_usd == pytest.approx(99.0)
        assert foreign_row.inflight_trial_count == 1

        assert result.totals.user_count - f.baseline.user_count == 2
    finally:
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(
                    TrialModel.task_id == foreign_task_id
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == foreign_task_id)
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id == f"exp_{foreign_task_id}"
                )
            )
            await session.execute(
                QuotaModel.__table__.delete().where(
                    QuotaModel.org_id == foreign_org_id
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == foreign_org_id
                )
            )
