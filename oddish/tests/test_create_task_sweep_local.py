"""Test that local-mode sweeps leave execution to the canonical queue worker.

When ``ODDISH_LOCAL_MODE=1``, probe trials submitted via the sweep endpoint
should only enqueue worker jobs. The backend lifespan owns the in-process queue
worker, so endpoint code must not also dispatch the legacy local runner.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.schemas import TaskSweepSubmission, AgentModelPair  # noqa: E402
from oddish.core.endpoints import create_task_sweep_core  # noqa: E402
from oddish.db import (  # noqa: E402
    TaskModel,
    TrialModel,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
)


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
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id.in_(
                    select(TrialModel.id).where(TrialModel.task_id == task_id)
                )
            )
        )
        await s.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await s.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))


@pytest.mark.asyncio
async def test_local_mode_sweep_only_enqueues_worker_job(monkeypatch, seeded_task_id):
    monkeypatch.setenv("ODDISH_LOCAL_MODE", "1")
    # Reload settings if cached
    import oddish.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "settings", cfg_mod.Settings())

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
        _, new_trials, _, _ = await create_task_sweep_core(
            s, submission=submission, org_id=None
        )

    assert len(new_trials) == 1
    async with get_session() as s:
        job = await s.scalar(
            select(WorkerJobModel).where(WorkerJobModel.subject_id == new_trials[0].id)
        )

    assert job is not None
    assert job.kind == WorkerJobKind.TRIAL
    assert job.status == WorkerJobStatus.QUEUED
