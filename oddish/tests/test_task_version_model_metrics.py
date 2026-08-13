"""Per-(task version, agent, model) metric rollups.

Exercises the recompute against a real database rather than a compiled query,
because the properties that matter -- percentile_disc's observed median, NULL
step handling, and the pass/fail split -- are Postgres semantics, not ORM ones.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oddish.core.task_version_model_metrics import (
    refresh_task_version_model_metrics,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TaskVersionModelMetricsModel,
    TrialModel,
    TrialStatus,
)


async def _seed(session, trials: list[dict]) -> tuple[str, str]:
    """Create a task + version and the given trials. Returns (task, version)."""
    suffix = uuid.uuid4().hex[:8]
    task_id = f"task-{suffix}"
    version_id = f"tv-{suffix}"
    experiment_id = f"exp-{suffix}"

    session.add(
        ExperimentModel(id=experiment_id, name=experiment_id, org_id="org-1")
    )
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id="org-1",
            user="tester",
            task_path="s3://test-bucket/tvm",
            status=TaskStatus.COMPLETED,
        )
    )
    # No relationship() links these, so SQLAlchemy will not order the inserts
    # by the foreign key on its own.
    await session.flush()
    session.add(
        TaskVersionModel(
            id=version_id,
            task_id=task_id,
            version=1,
            task_path="s3://test-bucket/tvm",
        )
    )
    await session.flush()

    for index, spec in enumerate(trials):
        trial_id = f"{task_id}-{index}"
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=experiment_id,
                org_id="org-1",
                agent=spec.pop("agent", "claude-code"),
                model=spec.pop("model", "claude-opus-4-8"),
                provider=spec.pop("provider", "anthropic"),
                queue_key="anthropic/claude-opus-4-8",
                is_probe=False,
                status=spec.pop("status", TrialStatus.SUCCESS),
                **spec,
            )
        )
    await session.flush()
    return task_id, version_id


async def _row(session, version_id: str, agent="claude-code", model="claude-opus-4-8"):
    result = await session.execute(
        select(TaskVersionModelMetricsModel).where(
            TaskVersionModelMetricsModel.task_version_id == version_id,
            TaskVersionModelMetricsModel.agent == agent,
            TaskVersionModelMetricsModel.model == model,
        )
    )
    return result.scalar_one_or_none()


@pytest.mark.asyncio
async def test_buckets_and_step_distribution(session):
    """Pass/fail split, and min/median/max over each outcome separately."""
    _, version_id = await _seed(
        session,
        [
            {"reward": 1.0, "total_steps": 10},
            {"reward": 1.0, "total_steps": 30},
            {"reward": 1.0, "total_steps": 20},
            {"reward": 0.0, "total_steps": 200},
            {"reward": 0.0, "total_steps": 300},
            {"reward": 0.5, "total_steps": 77},
        ],
    )
    await refresh_task_version_model_metrics(session, [version_id])
    row = await _row(session, version_id)

    assert (row.n_pass, row.n_fail, row.n_partial) == (3, 2, 1)

    # percentile_disc returns an observed value: 10/20/30 -> 20.
    assert (row.steps_pass_n, row.steps_pass_min, row.steps_pass_max) == (3, 10, 30)
    assert row.steps_pass_p50 == 20

    # Even-sized group: _disc takes the lower of 200/300, never 250.
    assert (row.steps_fail_n, row.steps_fail_min, row.steps_fail_max) == (2, 200, 300)
    assert row.steps_fail_p50 == 200

    # Partial carries a count only -- no distribution columns (decision 11).
    assert row.steps_partial_n == 1

    # The `all` block spans every outcome including the partial.
    assert (row.steps_all_min, row.steps_all_max) == (10, 300)
    assert row.n_steps_present == 6


@pytest.mark.asyncio
async def test_null_steps_are_excluded_not_zeroed(session):
    """A missing total_steps must not read as a zero-step trial."""
    _, version_id = await _seed(
        session,
        [
            {"reward": 1.0, "total_steps": 40},
            {"reward": 1.0, "total_steps": None},
            {"reward": 1.0, "total_steps": None},
        ],
    )
    await refresh_task_version_model_metrics(session, [version_id])
    row = await _row(session, version_id)

    assert row.n_pass == 3
    # Three passing trials, one measured: stats describe the one.
    assert row.steps_pass_n == 1
    assert row.steps_pass_min == row.steps_pass_max == row.steps_pass_p50 == 40


@pytest.mark.asyncio
async def test_group_with_no_steps_yields_null_not_zero(session):
    """Never-measured must be NULL, so a filter excludes it rather than ranking it."""
    _, version_id = await _seed(
        session, [{"reward": 1.0, "total_steps": None}, {"reward": 0.0}]
    )
    await refresh_task_version_model_metrics(session, [version_id])
    row = await _row(session, version_id)

    assert row.n_steps_present == 0
    assert row.steps_all_min is None
    assert row.steps_all_p50 is None
    assert row.steps_pass_min is None
    assert row.steps_pass_n == 0


@pytest.mark.asyncio
async def test_inflight_trials_stay_out_of_scored_counts(session):
    """A running trial must not move any pass/fail number."""
    _, version_id = await _seed(
        session,
        [
            {"reward": 1.0, "total_steps": 5},
            {"status": TrialStatus.RUNNING},
            {"status": TrialStatus.QUEUED},
        ],
    )
    await refresh_task_version_model_metrics(session, [version_id])
    row = await _row(session, version_id)

    assert row.n_pass == 1
    assert row.n_fail == 0
    assert row.n_inflight == 2


@pytest.mark.asyncio
async def test_grain_splits_by_agent_and_model(session):
    """Same task version, two models -> two rows, not one blended one."""
    _, version_id = await _seed(
        session,
        [
            {"model": "claude-opus-4-8", "reward": 1.0, "total_steps": 10},
            {"model": "claude-opus-4-8", "reward": 1.0, "total_steps": 12},
            {"model": "gpt-5.5", "reward": 0.0, "total_steps": 400},
        ],
    )
    await refresh_task_version_model_metrics(session, [version_id])

    opus = await _row(session, version_id, model="claude-opus-4-8")
    gpt = await _row(session, version_id, model="gpt-5.5")
    assert (opus.n_pass, opus.n_fail) == (2, 0)
    assert (gpt.n_pass, gpt.n_fail) == (0, 1)
    assert opus.steps_pass_max == 12
    assert gpt.steps_fail_min == 400


@pytest.mark.asyncio
async def test_recompute_is_idempotent(session):
    """Running twice yields identical rows -- the property deltas would break."""
    _, version_id = await _seed(
        session, [{"reward": 1.0, "total_steps": 8}, {"reward": 0.0, "total_steps": 9}]
    )
    await refresh_task_version_model_metrics(session, [version_id])
    first = await _row(session, version_id)
    snapshot = (first.n_pass, first.n_fail, first.steps_pass_p50, first.sum_steps)

    await refresh_task_version_model_metrics(session, [version_id])
    second = await _row(session, version_id)
    assert (
        second.n_pass,
        second.n_fail,
        second.steps_pass_p50,
        second.sum_steps,
    ) == snapshot


@pytest.mark.asyncio
async def test_trial_moving_backwards_leaves_its_bucket(session):
    """A retried trial is reset to RUNNING with reward and steps nulled."""
    task_id, version_id = await _seed(
        session, [{"reward": 1.0, "total_steps": 15}, {"reward": 1.0, "total_steps": 25}]
    )
    await refresh_task_version_model_metrics(session, [version_id])
    assert (await _row(session, version_id)).n_pass == 2

    trial = await session.get(TrialModel, f"{task_id}-0")
    trial.status = TrialStatus.RUNNING
    trial.reward = None
    trial.total_steps = None
    await session.flush()

    await refresh_task_version_model_metrics(session, [version_id])
    row = await _row(session, version_id)
    assert row.n_pass == 1
    assert row.n_inflight == 1
    # The retired trial's steps must leave the distribution too.
    assert row.steps_pass_n == 1
    assert row.steps_pass_min == 25


@pytest.mark.asyncio
async def test_soft_deleted_and_superseded_trials_drop_out(session):
    """Scope predicate is applied by the recompute's own SQL, not an ORM listener."""
    task_id, version_id = await _seed(
        session,
        [
            {"reward": 1.0, "total_steps": 3},
            {"reward": 1.0, "total_steps": 4},
            {"reward": 1.0, "total_steps": 5},
        ],
    )
    from datetime import datetime, timezone

    deleted = await session.get(TrialModel, f"{task_id}-1")
    deleted.deleted_at = datetime.now(timezone.utc)
    superseded = await session.get(TrialModel, f"{task_id}-2")
    superseded.superseded_by_trial_id = f"{task_id}-0"
    await session.flush()

    await refresh_task_version_model_metrics(session, [version_id])
    row = await _row(session, version_id)
    assert row.n_pass == 1
    assert row.steps_pass_n == 1
    assert row.steps_pass_min == 3


@pytest.mark.asyncio
async def test_group_losing_its_last_trial_is_deleted(session):
    """A stale row must not survive its population going empty."""
    task_id, version_id = await _seed(session, [{"reward": 1.0, "total_steps": 6}])
    await refresh_task_version_model_metrics(session, [version_id])
    assert await _row(session, version_id) is not None

    from datetime import datetime, timezone

    trial = await session.get(TrialModel, f"{task_id}-0")
    trial.deleted_at = datetime.now(timezone.utc)
    await session.flush()

    await refresh_task_version_model_metrics(session, [version_id])
    assert await _row(session, version_id) is None


# --- backfill ---------------------------------------------------------------
#
# The backfill drives its own sessions and commits, so these use committed rows
# rather than the rolled-back `session` fixture and clean up after themselves.


@pytest.mark.asyncio
async def test_backfill_covers_versions_and_skips_empty_ones(session):
    """A version with no in-scope trials yields no row and must not stall the loop.

    A "select versions missing a row" cursor would hand back the trial-less
    version forever; this is the guard on the keyset pagination that replaced it.
    """
    from oddish.core.backfill_task_version_model_metrics import backfill

    _, with_trials = await _seed(session, [{"reward": 1.0, "total_steps": 11}])
    _, without_trials = await _seed(session, [])
    await session.commit()

    try:
        processed = await backfill(batch=5)
        assert processed > 0

        assert await _row(session, with_trials) is not None
        assert await _row(session, without_trials) is None
    finally:
        await session.rollback()


@pytest.mark.asyncio
async def test_backfill_is_idempotent(session):
    """Re-running must not duplicate or change rows."""
    from sqlalchemy import func as sa_func

    from oddish.core.backfill_task_version_model_metrics import backfill

    await _seed(session, [{"reward": 1.0, "total_steps": 21}, {"reward": 0.0}])
    await session.commit()

    async def _count() -> int:
        return (
            await session.execute(
                select(sa_func.count()).select_from(TaskVersionModelMetricsModel)
            )
        ).scalar_one()

    try:
        await backfill(batch=5)
        first = await _count()
        assert first > 0

        await backfill(batch=5)
        session.expire_all()
        assert await _count() == first
    finally:
        await session.rollback()
