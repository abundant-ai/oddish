"""Test that create_task_sweep_core dispatches to the local runner when LOCAL_MODE=1.

When ``ODDISH_LOCAL_MODE=1``, probe trials submitted via the sweep endpoint
should fire ``run_trial_locally(trial.id, dry_run=False)`` as a background
asyncio task instead of going through the Modal queue. This test sets the env
var, monkeypatches the runner, submits a sweep, and asserts the runner was
called once per new trial.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.schemas import TaskSweepSubmission, AgentModelPair  # noqa: E402
from oddish.core.endpoints import create_task_sweep_core  # noqa: E402
from oddish.db import TaskModel, TrialModel, get_session  # noqa: E402


@pytest_asyncio.fixture
async def seeded_task_id(tmp_path):
    """Insert a TaskModel and return its id. Cleanup after."""
    task_id = "test_sweep_local_task"
    task_dir = tmp_path / "fake-task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("solve")
    (task_dir / "task.toml").write_text('version = "1.0"\n')

    async with get_session() as s:
        s.add(
            TaskModel(
                id=task_id,
                name="sweep-local-test",
                user="test",
                task_path=str(task_dir),
            )
        )
    yield task_id
    async with get_session() as s:
        await s.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await s.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))


@pytest.mark.asyncio
async def test_local_mode_dispatches_local_runner(monkeypatch, seeded_task_id):
    """When ODDISH_LOCAL_MODE=1, create_task_sweep_core should call run_trial_locally
    for each new trial via asyncio.create_task (not the Modal enqueue path).
    """
    monkeypatch.setenv("ODDISH_LOCAL_MODE", "1")
    # Reload settings if cached
    import oddish.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "settings", cfg_mod.Settings())

    fake = AsyncMock()
    monkeypatch.setattr("oddish.worker.local_runner.run_trial_locally", fake)

    submission = TaskSweepSubmission(
        task_id=seeded_task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=1,
            )
        ],
        user="alice",
        extra_instructions="cheat",
    )

    async with get_session() as s:
        task, new_trials, is_append, exp = await create_task_sweep_core(
            s, submission=submission, org_id=None
        )

    # asyncio.create_task fires fire-and-forget; await one tick so the task runs
    await asyncio.sleep(0.05)

    assert fake.await_count == len(new_trials)
    assert fake.await_count >= 1
    fake.assert_any_await(new_trials[0].id, dry_run=False)
