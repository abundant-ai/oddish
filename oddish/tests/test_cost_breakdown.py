"""DB-backed tests for the global admin cost breakdown over billable trials."""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.admin import get_cost_breakdown_core  # noqa: E402
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    utcnow,
)
from oddish.model_pricing import estimate_cost_usd  # noqa: E402

_RUN = uuid.uuid4().hex[:8]

_EST_MODEL = "gpt-5.5-pro"
_EST_IN, _EST_OUT, _EST_CACHE = 10_000, 4_000, 1_000
_EXPECTED_EST = estimate_cost_usd(_EST_MODEL, _EST_IN, _EST_OUT, _EST_CACHE)

USER_A = f"costuser-a-{_RUN}"
USER_B = f"costuser-b-{_RUN}"
ORG_1 = f"costorg-1-{_RUN}"
ORG_2 = f"costorg-2-{_RUN}"
E1 = f"costexp-1-{_RUN}"
E2 = f"costexp-2-{_RUN}"
E3 = f"costexp-3-{_RUN}"
E4 = f"costexp-deleted-{_RUN}"
E5 = f"costexp-nonbillable-{_RUN}"


def _approx(a: float | None, b: float | None, tol: float = 1e-6) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


@pytest_asyncio.fixture
async def seeded_cost_data():
    """Insert experiments, tasks, and trials, then hard-delete them after."""
    now = utcnow()
    recent = now - timedelta(hours=1)
    old = now - timedelta(days=40)

    async with get_session() as session:
        session.add_all(
            [
                ExperimentModel(
                    id=E1,
                    name="cost-exp-one",
                    org_id=ORG_1,
                    owner_user_id=USER_A,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E2,
                    name="cost-exp-two",
                    org_id=ORG_1,
                    owner_user_id=USER_B,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E3,
                    name="cost-exp-three",
                    org_id=ORG_2,
                    owner_user_id=EXPERIMENTS_UNATTRIBUTED_OWNER,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E4,
                    name="cost-exp-deleted",
                    org_id=ORG_1,
                    owner_user_id=USER_A,
                    created_at=recent,
                    last_activity_at=recent,
                    deleted_at=now,
                ),
                ExperimentModel(
                    id=E5,
                    name="cost-exp-nonbillable",
                    org_id=ORG_2,
                    owner_user_id=USER_A,
                    created_at=recent,
                    last_activity_at=recent,
                ),
            ]
        )
        for exp_id, org_id in (
            (E1, ORG_1),
            (E2, ORG_1),
            (E3, ORG_2),
            (E4, ORG_1),
            (E5, ORG_2),
        ):
            session.add(
                TaskModel(
                    id=f"{exp_id}-task",
                    name=f"{exp_id}-task",
                    user="test",
                    org_id=org_id,
                    task_path="some/path",
                )
            )
        session.add_all(
            [
                _trial(
                    E1,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=2.0,
                    created_at=recent,
                    billed_user_id=USER_A,
                ),
                _trial(
                    E1,
                    1,
                    model="claude-opus-4-8",
                    cost_usd=3.0,
                    created_at=recent,
                    billed_user_id=USER_B,
                ),
                _trial(
                    E2,
                    0,
                    model=_EST_MODEL,
                    provider="openai",
                    agent="codex",
                    cost_usd=None,
                    input_tokens=_EST_IN,
                    output_tokens=_EST_OUT,
                    cache_tokens=_EST_CACHE,
                    created_at=recent,
                    billed_user_id=USER_B,
                ),
                _trial(
                    E3,
                    0,
                    model="Claude-Opus-4-8",
                    cost_usd=1.5,
                    created_at=recent,
                    billed_user_id=USER_A,
                ),
                _trial(
                    E4,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=99.0,
                    created_at=recent,
                    billed_user_id=USER_A,
                ),
                _trial(
                    E1,
                    2,
                    model="claude-opus-4-8",
                    cost_usd=50.0,
                    created_at=recent,
                    deleted_at=now,
                ),
                _trial(
                    E1,
                    3,
                    model="claude-opus-4-8",
                    cost_usd=7.0,
                    created_at=old,
                    billed_user_id=USER_A,
                ),
                _trial(E5, 0, model="claude-opus-4-8", cost_usd=4.0, created_at=recent),
            ]
        )

    yield

    async with get_session() as session:
        for exp_id in (E1, E2, E3, E4, E5):
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.experiment_id == exp_id)
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == f"{exp_id}-task")
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(ExperimentModel.id == exp_id)
            )


def _trial(
    experiment_id: str,
    index: int,
    *,
    model: str | None,
    cost_usd: float | None,
    created_at,
    provider: str = "bedrock",
    agent: str = "claude-code",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_tokens: int | None = None,
    billed_user_id: str | None = None,
    deleted_at=None,
) -> TrialModel:
    return TrialModel(
        id=f"{experiment_id}-{index}",
        name=f"{experiment_id}-{index}",
        task_id=f"{experiment_id}-task",
        experiment_id=experiment_id,
        org_id=None,
        agent=agent,
        provider=provider,
        queue_key=f"{provider}/{model or 'default'}",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        billed_user_id=billed_user_id,
        cost_usd=cost_usd,
        created_at=created_at,
        deleted_at=deleted_at,
    )


@pytest.mark.asyncio
async def test_cost_breakdown_window_attribution_and_soft_delete(seeded_cost_data):
    assert _EXPECTED_EST is not None and _EXPECTED_EST > 0

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )

    exps = {e.experiment_id: e for e in result.experiments}

    assert E4 not in exps
    assert E5 not in exps

    assert _approx(exps[E1].cost_usd, 5.0), exps[E1].cost_usd
    assert _approx(exps[E1].cost_estimated_usd, 0.0)
    assert exps[E1].trial_count == 2
    assert exps[E1].models[0].model == "claude-opus-4-8"
    assert _approx(exps[E1].models[0].cost_usd, 5.0)

    assert _approx(exps[E2].cost_usd, _EXPECTED_EST), exps[E2].cost_usd
    assert _approx(exps[E2].cost_estimated_usd, _EXPECTED_EST)
    assert exps[E2].models[0].model == _EST_MODEL
    assert exps[E2].input_tokens == _EST_IN

    assert _approx(exps[E3].cost_usd, 1.5)
    assert exps[E3].models[0].model == "claude-opus-4-8"
    assert exps[E3].owner_user_id is None

    by_user = {u.owner_user_id: u for u in result.by_user}
    assert _approx(by_user[USER_A].cost_usd, 3.5)
    assert by_user[USER_A].experiment_count == 2
    assert by_user[USER_A].trial_count == 2
    assert _approx(by_user[USER_B].cost_usd, 3.0 + _EXPECTED_EST)
    assert by_user[USER_B].experiment_count == 2
    assert None not in by_user

    costs = [e.cost_usd for e in result.experiments]
    assert costs == sorted(costs, reverse=True)

    assert result.bucket == "day"
    for series in (
        result.series_by_agent,
        result.series_by_model,
        result.series_by_user,
    ):
        assert series.buckets, "expected at least one series bucket"
        assert _approx(
            sum(b.cost_usd for b in series.buckets), result.totals.cost_usd, tol=0.01
        )
        assert sum(b.trial_count for b in series.buckets) == result.totals.trial_count
        starts = [b.bucket_start for b in series.buckets]
        assert starts == sorted(starts)
        for b in series.buckets:
            assert _approx(sum(b.costs.values()), b.cost_usd, tol=0.01)

    agent_keys = {k.key for k in result.series_by_agent.keys}
    assert "claude-code" in agent_keys and "codex" in agent_keys

    model_keys = {k.key for k in result.series_by_model.keys}
    assert "claude-opus-4-8" in model_keys
    assert "Claude-Opus-4-8" not in model_keys

    user_keys = {k.key for k in result.series_by_user.keys}
    assert USER_A in user_keys and USER_B in user_keys
    assert "__unattributed__" not in user_keys


@pytest.mark.asyncio
async def test_cost_breakdown_excludes_non_billable_and_normalizes_sentinel(
    seeded_cost_data,
):
    """Non-billable trials drop out and the owner sentinel never reaches the UI."""
    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )

    exps = {e.experiment_id: e for e in result.experiments}

    assert E5 not in exps
    assert exps[E3].owner_user_id is None, exps[E3].owner_user_id

    assert all(e.owner_user_id != EXPERIMENTS_UNATTRIBUTED_OWNER for e in exps.values())
    owner_ids = {u.owner_user_id for u in result.by_user}
    assert EXPERIMENTS_UNATTRIBUTED_OWNER not in owner_ids
    assert None not in owner_ids

    assert all(k.key != "__unattributed__" for k in result.series_by_user.keys)


@pytest.mark.asyncio
async def test_cost_breakdown_all_time_includes_old_trials(seeded_cost_data):
    async with get_session() as session:
        windowed = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )
        all_time = await get_cost_breakdown_core(
            session, window_days=None, experiment_limit=500, user_limit=500
        )

    e1_windowed = {e.experiment_id: e for e in windowed.experiments}[E1]
    e1_all = {e.experiment_id: e for e in all_time.experiments}[E1]

    assert _approx(e1_windowed.cost_usd, 5.0)
    assert _approx(e1_all.cost_usd, 12.0)
    assert e1_all.trial_count == 3

    user_a_all = {u.owner_user_id: u for u in all_time.by_user}[USER_A]
    assert _approx(user_a_all.cost_usd, 10.5)
