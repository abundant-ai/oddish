"""Tests for the orphan-trial reaper."""

from __future__ import annotations

import uuid
from unittest.mock import patch, AsyncMock, MagicMock

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
from oddish.worker.orphan_reaper import reap_orphan_trials, REAP_REASON


@pytest_asyncio.fixture
async def seeded_running_trial(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    trial_id = f"trial_orphan_{suffix}"
    task_id = f"task_orphan_{suffix}"
    exp_id = f"exp_orphan_{suffix}"

    async with get_session() as session:
        session.add(ExperimentModel(id=exp_id, name=f"orphan-{suffix}"))
        session.add(
            TaskModel(
                id=task_id,
                name=f"orphan-task-{suffix}",
                user="test",
                task_path=str(tmp_path),
            )
        )
        session.add(
            TrialModel(
                id=trial_id,
                name=f"orphan-trial-{suffix}",
                task_id=task_id,
                experiment_id=exp_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-5",
                queue_key="test-orphan",
                status=TrialStatus.RUNNING,
                origin=TrialOrigin.ODDISH,
            )
        )

    yield trial_id, f"orphan-trial-{suffix}"

    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id == trial_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.id == exp_id)
        )


@pytest.mark.asyncio
async def test_reap_marks_orphan_running_trials_as_failed(seeded_running_trial):
    """A RUNNING trial whose container doesn't exist should be flipped to FAILED."""
    trial_id, _ = seeded_running_trial

    # Mock docker ps to return zero containers
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    with patch(
        "oddish.worker.orphan_reaper.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        reaped = await reap_orphan_trials()

    assert reaped >= 1
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        assert trial.status == TrialStatus.FAILED
        assert trial.finished_at is not None
        assert "orphan" in (trial.error_message or "").lower()


@pytest.mark.asyncio
async def test_reap_skips_trials_with_matching_container(seeded_running_trial):
    """A RUNNING trial WITH a matching container should be left alone."""
    trial_id, trial_name = seeded_running_trial

    # docker ps returns a container that matches the trial's expected suffix
    container_name = f"{trial_name.lower()}-main-1"
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(container_name.encode() + b"\n", b""))
    with patch(
        "oddish.worker.orphan_reaper.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        reaped = await reap_orphan_trials()

    # Other RUNNING trials in the DB might still get reaped, but ours shouldn't.
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        assert trial.status == TrialStatus.RUNNING


@pytest.mark.asyncio
async def test_reap_handles_docker_unavailable():
    """If `docker ps` fails, reaper returns 0 and doesn't crash."""
    with patch(
        "oddish.worker.orphan_reaper.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("docker not on PATH"),
    ):
        reaped = await reap_orphan_trials()
    assert reaped == 0
