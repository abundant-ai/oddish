"""Integration tests for the admin cost breakdown aggregation.

Exercises ``oddish.core.admin.get_cost_breakdown_core`` against a real
Postgres (``ODDISH_DATABASE_URL``), like the other DB-backed tests in this
suite. The aggregation is **global** (no org/experiment filter), so the
assertions key off uniquely-suffixed experiment ids and owner user ids that
this test creates -- never absolute org-wide totals, which would be perturbed
by any other rows in a shared test database.

Covered behaviors:

* native ``cost_usd`` is summed, and trials lacking it are priced from token
  counts via the per-model estimate (the ``cost_estimated_usd`` split);
* per-user attribution flows through ``experiments.owner_user_id`` (including
  the unattributed ``None`` owner);
* soft-deleted experiments (and soft-deleted trials) are excluded;
* the trailing-window filter bounds the rollups;
* model ids are normalized so spellings (case/whitespace) collapse onto one row;
* the time-bucketed ``series`` reconciles with the windowed totals; and
* experiments rank by descending cost.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.admin import get_cost_breakdown_core  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    utcnow,
)
from oddish.model_pricing import estimate_cost_usd  # noqa: E402

_RUN = uuid.uuid4().hex[:8]

# A model the pricing table can resolve, so the estimate is deterministic.
_EST_MODEL = "gpt-5.5-pro"
_EST_IN, _EST_OUT, _EST_CACHE = 10_000, 4_000, 1_000
_EXPECTED_EST = estimate_cost_usd(_EST_MODEL, _EST_IN, _EST_OUT, _EST_CACHE)

# Distinct, run-scoped identifiers so the global aggregation can be filtered
# back down to just this test's rows.
USER_A = f"costuser-a-{_RUN}"
USER_B = f"costuser-b-{_RUN}"
USER_C = f"costuser-c-{_RUN}"
ORG_1 = f"costorg-1-{_RUN}"
ORG_2 = f"costorg-2-{_RUN}"
ORG_3 = f"costorg-3-{_RUN}"
E1 = f"costexp-1-{_RUN}"
E2 = f"costexp-2-{_RUN}"
E3 = f"costexp-3-{_RUN}"
E4 = f"costexp-deleted-{_RUN}"
# E5 exercises the QA / probe 3-way split; isolated under its own user/org so
# it doesn't perturb the USER_A/USER_B attribution assertions above.
E5 = f"costexp-qa-{_RUN}"
# Per-trial QA (analysis) and per-task verdict costs seeded on E5.
_E5_REAL_EXEC = 4.0
_E5_PROBE_EXEC = 1.0
_E5_ANALYSIS_REAL = 0.10
_E5_ANALYSIS_PROBE = 0.05
_E5_VERDICT = 0.02


def _approx(a: float | None, b: float | None, tol: float = 1e-6) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


@pytest_asyncio.fixture
async def seeded_cost_data():
    """Insert experiments/tasks/trials, yield, then hard-delete them.

    Cleanup uses Core table deletes (bypassing the ORM soft-delete filter) so
    the soft-deleted experiment/trial are removed too; the ``trials`` FK
    cascades, but we delete trials explicitly first to be safe.
    """
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
                    owner_user_id=None,
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
                    name="cost-exp-qa",
                    org_id=ORG_3,
                    owner_user_id=USER_C,
                    created_at=recent,
                    last_activity_at=recent,
                ),
            ]
        )
        for exp_id, org_id in ((E1, ORG_1), (E2, ORG_1), (E3, ORG_2), (E4, ORG_1)):
            session.add(
                TaskModel(
                    id=f"{exp_id}-task",
                    name=f"{exp_id}-task",
                    user="test",
                    org_id=org_id,
                    task_path="some/path",
                )
            )
        # E5's task carries a verdict-synthesis cost (the per-task half of QA).
        session.add(
            TaskModel(
                id=f"{E5}-task",
                name=f"{E5}-task",
                user="test",
                org_id=ORG_3,
                task_path="some/path",
                verdict_cost_usd=_E5_VERDICT,
            )
        )
        session.add_all(
            [
                _trial(E1, 0, model="claude-opus-4-8", cost_usd=2.0, created_at=recent),
                _trial(E1, 1, model="claude-opus-4-8", cost_usd=3.0, created_at=recent),
                # No native cost -> priced from tokens. Distinct agent so the
                # by-agent series has more than one stack.
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
                ),
                # Mixed-case model id -> must normalize to "claude-opus-4-8".
                _trial(E3, 0, model="Claude-Opus-4-8", cost_usd=1.5, created_at=recent),
                # Excluded: trial of a soft-deleted experiment.
                _trial(
                    E4, 0, model="claude-opus-4-8", cost_usd=99.0, created_at=recent
                ),
                # Excluded: soft-deleted trial.
                _trial(
                    E1,
                    2,
                    model="claude-opus-4-8",
                    cost_usd=50.0,
                    created_at=recent,
                    deleted_at=now,
                ),
                # Old trial in E1: out of the 7d window, in all-time.
                _trial(E1, 3, model="claude-opus-4-8", cost_usd=7.0, created_at=old),
                # E5: one real + one probe trial, each with a per-trial QA
                # (analysis) cost; the task also carries a verdict cost above.
                _trial(
                    E5,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=_E5_REAL_EXEC,
                    analysis_cost_usd=_E5_ANALYSIS_REAL,
                    created_at=recent,
                ),
                _trial(
                    E5,
                    1,
                    model="claude-opus-4-8",
                    cost_usd=_E5_PROBE_EXEC,
                    analysis_cost_usd=_E5_ANALYSIS_PROBE,
                    is_probe=True,
                    created_at=recent,
                ),
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
    deleted_at=None,
    is_probe: bool = False,
    analysis_cost_usd: float | None = None,
    task_id: str | None = None,
) -> TrialModel:
    return TrialModel(
        id=f"{experiment_id}-{index}",
        name=f"{experiment_id}-{index}",
        task_id=task_id or f"{experiment_id}-task",
        experiment_id=experiment_id,
        org_id=None,
        agent=agent,
        provider=provider,
        queue_key=f"{provider}/{model or 'default'}",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        cost_usd=cost_usd,
        created_at=created_at,
        deleted_at=deleted_at,
        is_probe=is_probe,
        analysis_cost_usd=analysis_cost_usd,
    )


@pytest.mark.asyncio
async def test_cost_breakdown_window_attribution_and_soft_delete(seeded_cost_data):
    assert _EXPECTED_EST is not None and _EXPECTED_EST > 0

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )

    exps = {e.experiment_id: e for e in result.experiments}

    # Soft-deleted experiment's trial is excluded entirely.
    assert E4 not in exps

    # Native cost is summed; the soft-deleted 50.0 trial does not inflate E1.
    assert _approx(exps[E1].cost_usd, 5.0), exps[E1].cost_usd
    assert _approx(exps[E1].cost_estimated_usd, 0.0)
    assert exps[E1].trial_count == 2
    assert exps[E1].models[0].model == "claude-opus-4-8"
    assert _approx(exps[E1].models[0].cost_usd, 5.0)

    # No native cost -> fully estimated from tokens.
    assert _approx(exps[E2].cost_usd, _EXPECTED_EST), exps[E2].cost_usd
    assert _approx(exps[E2].cost_estimated_usd, _EXPECTED_EST)
    assert exps[E2].models[0].model == _EST_MODEL
    assert exps[E2].input_tokens == _EST_IN

    # Unattributed-owner experiment still aggregates, and its mixed-case model
    # id is normalized to the canonical lowercase form.
    assert _approx(exps[E3].cost_usd, 1.5)
    assert exps[E3].models[0].model == "claude-opus-4-8"

    # Per-user attribution via experiment ownership.
    by_user = {u.owner_user_id: u for u in result.by_user}
    assert _approx(by_user[USER_A].cost_usd, 5.0)
    assert by_user[USER_A].experiment_count == 1
    assert by_user[USER_A].trial_count == 2
    assert _approx(by_user[USER_B].cost_usd, _EXPECTED_EST)

    # Experiments are ranked by descending cost.
    costs = [e.cost_usd for e in result.experiments]
    assert costs == sorted(costs, reverse=True)

    # All three chart series (stacked by agent / model / user) reconcile with
    # the windowed totals (all global over the same window, so this holds
    # regardless of other rows in a shared DB) and are chronological. Tolerance
    # covers per-bucket rounding.
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
        # Each bucket's per-key split sums to that bucket's total.
        for b in series.buckets:
            assert _approx(sum(b.costs.values()), b.cost_usd, tol=0.01)

    # by-agent series splits the two agents used in the fixtures.
    agent_keys = {k.key for k in result.series_by_agent.keys}
    assert "claude-code" in agent_keys and "codex" in agent_keys

    # by-model series keys are normalized (E3's mixed-case model merged in).
    model_keys = {k.key for k in result.series_by_model.keys}
    assert "claude-opus-4-8" in model_keys
    assert "Claude-Opus-4-8" not in model_keys

    # by-user series carries both owners plus the unattributed (E3) bucket.
    user_keys = {k.key for k in result.series_by_user.keys}
    assert USER_A in user_keys and USER_B in user_keys
    assert "__unattributed__" in user_keys


@pytest.mark.asyncio
async def test_cost_breakdown_qa_and_probe_split(seeded_cost_data):
    """E5 partitions spend into real exec / probe exec / QA (analysis+verdict)."""
    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )

    e5 = {e.experiment_id: e for e in result.experiments}[E5]

    # Execution split: real and probe partition cost_usd.
    assert _approx(e5.cost_real_execution_usd, _E5_REAL_EXEC), e5.cost_real_execution_usd
    assert _approx(e5.cost_probe_execution_usd, _E5_PROBE_EXEC)
    assert _approx(e5.cost_usd, _E5_REAL_EXEC + _E5_PROBE_EXEC)

    # QA = per-trial analysis (real + probe) + the per-task verdict cost.
    expected_qa = _E5_ANALYSIS_REAL + _E5_ANALYSIS_PROBE + _E5_VERDICT
    assert _approx(e5.cost_qa_usd, expected_qa), e5.cost_qa_usd

    # Grand total is execution + QA.
    assert _approx(e5.cost_total_usd, _E5_REAL_EXEC + _E5_PROBE_EXEC + expected_qa)

    # ``cost_usd`` stays execution-only for back-compat (no QA double-count).
    assert _approx(e5.cost_usd, _E5_REAL_EXEC + _E5_PROBE_EXEC)
    assert e5.trial_count == 2

    # The windowed totals include E5's contribution to each split bucket. Use
    # >= since the global aggregation may include unrelated rows in a shared DB.
    assert result.totals.cost_probe_execution_usd >= _E5_PROBE_EXEC - 1e-6
    assert result.totals.cost_qa_usd >= expected_qa - 1e-6
    # QA verdict and analysis halves both land in the totals.
    assert result.totals.cost_qa_verdict_usd >= _E5_VERDICT - 1e-6
    assert result.totals.cost_qa_analysis_usd >= (
        _E5_ANALYSIS_REAL + _E5_ANALYSIS_PROBE - 1e-6
    )
    # Total reconciles: execution (real+probe) + QA.
    assert _approx(
        result.totals.cost_total_usd,
        result.totals.cost_usd + result.totals.cost_qa_usd,
        tol=0.01,
    )


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

    # The 40-day-old 7.0 trial is excluded from 7d but included in all-time.
    assert _approx(e1_windowed.cost_usd, 5.0)
    assert _approx(e1_all.cost_usd, 12.0)
    assert e1_all.trial_count == 3

    user_a_all = {u.owner_user_id: u for u in all_time.by_user}[USER_A]
    assert _approx(user_a_all.cost_usd, 12.0)
