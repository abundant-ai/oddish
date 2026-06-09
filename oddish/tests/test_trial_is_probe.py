"""create_task must set TrialModel.is_probe from the probe marker so the
indexed column tracks harbor_config.mode."""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import TaskModel, TrialModel, get_session  # noqa: E402
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            # ON DELETE CASCADE removes trials + task_versions with the task.
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


def _submission(*, name: str, extra_instructions: str | None) -> TaskSubmission:
    # An s3:// task_path skips create_task's local upload + timeout validation,
    # so the test needs no real storage and no task.toml on disk.
    return TaskSubmission(
        name=f"{name}-{_RUN}",
        task_path="s3://test-bucket/is-probe-fake-task",
        user="test",
        trials=[TrialSpec(agent="nop", model=None)],
        extra_instructions=extra_instructions,
    )


@pytest.mark.asyncio
async def test_create_task_marks_probe_trials(cleanup_task_ids):
    async with get_session() as session:
        probe_task = await create_task(
            session, _submission(name="is-probe-test-probe", extra_instructions="poke around")
        )
        normal_task = await create_task(
            session, _submission(name="is-probe-test-normal", extra_instructions=None)
        )
    cleanup_task_ids.extend([probe_task.id, normal_task.id])

    async with get_session() as session:
        probe_trials = (
            await session.execute(
                TrialModel.__table__.select().where(
                    TrialModel.task_id == probe_task.id
                )
            )
        ).all()
        normal_trials = (
            await session.execute(
                TrialModel.__table__.select().where(
                    TrialModel.task_id == normal_task.id
                )
            )
        ).all()

    assert probe_trials and all(row.is_probe for row in probe_trials)
    assert normal_trials and all(not row.is_probe for row in normal_trials)
