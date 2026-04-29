"""Tests for ``worker.local_runner``.

These touch the local Postgres database through ``oddish.db.get_session``.
Run with ``.env.local`` sourced so ``ODDISH_DATABASE_URL`` points at the
local stack:

    set -a && source .env.local && set +a && uv run pytest tests/test_local_runner.py -v
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
)

from worker.local_runner import run_trial_locally


@pytest_asyncio.fixture
async def seeded_trial_id(tmp_path):
    """Insert a minimal Experiment + Task + Trial (status=QUEUED).

    Yields the trial id and cleans up all three rows afterwards. Uses
    short uuid suffixes to avoid clashing with seed rows or other tests.
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp_lr_{suffix}"
    task_id = f"task_lr_{suffix}"
    trial_id = f"trial_lr_{suffix}"

    async with get_session() as session:
        session.add(
            ExperimentModel(
                id=experiment_id,
                name=f"local-runner-test-{suffix}",
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                name=f"local-runner-task-{suffix}",
                user="test",
                task_path=str(tmp_path),
            )
        )
        session.add(
            TrialModel(
                id=trial_id,
                name=f"{task_id}-0",
                task_id=task_id,
                experiment_id=experiment_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-5",
                queue_key="test-local-runner",
                status=TrialStatus.QUEUED,
                origin=TrialOrigin.ODDISH,
            )
        )

    yield trial_id

    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id == trial_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )


@pytest.mark.asyncio
async def test_run_trial_locally_dry_run_marks_success(seeded_trial_id):
    """Dry-run path should QUEUED -> RUNNING -> SUCCESS and set timestamps."""
    await run_trial_locally(seeded_trial_id, dry_run=True)

    async with get_session() as session:
        trial = await session.get(TrialModel, seeded_trial_id)
        assert trial is not None
        assert trial.status == TrialStatus.SUCCESS
        assert trial.started_at is not None
        assert trial.finished_at is not None
        assert trial.finished_at >= trial.started_at


@pytest.mark.asyncio
async def test_run_trial_locally_missing_trial_raises():
    with pytest.raises(ValueError, match="not found"):
        await run_trial_locally("nonexistent-trial-id", dry_run=True)
