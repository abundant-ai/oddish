"""A retry must wait for the task row lock before minting ``{task_id}-{N}``."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.trials import retry_trial_core  # noqa: E402
from oddish.db import (  # noqa: E402
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    get_session,
)
from oddish.queue import create_task, reserve_next_trial_index  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402


@pytest_asyncio.fixture
async def cleanup_task_ids():
    task_ids: list[str] = []
    yield task_ids
    async with get_session() as session:
        for task_id in task_ids:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id.like(f"{task_id}%")
                )
            )
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
            )
            experiment_ids = [
                row[0]
                for row in await session.execute(
                    text(
                        "SELECT experiment_id FROM task_experiments"
                        " WHERE task_id = :task_id"
                    ),
                    {"task_id": task_id},
                )
            ]
            await session.execute(
                text("DELETE FROM task_experiments WHERE task_id = :task_id"),
                {"task_id": task_id},
            )
            if experiment_ids:
                await session.execute(
                    text(
                        "DELETE FROM experiments WHERE id = ANY(:ids)"
                        " OR shadow_of = ANY(:ids)"
                    ),
                    {"ids": experiment_ids},
                )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


@pytest.mark.asyncio
async def test_retry_waits_for_the_task_row_lock(cleanup_task_ids):
    task_id = f"retry-lock-{uuid.uuid4().hex[:8]}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session,
            TaskSubmission(
                name="retry-lock",
                task_path="s3://test-bucket/retry-lock-fake-task",
                trials=[TrialSpec(agent="nop", model=None)],
            ),
            task_id=task_id,
            org_id=None,
            billed_user_id=None,
        )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.status = TrialStatus.FAILED
        trial.finished_at = datetime.now(timezone.utc)
        # create_task mints more than the one agent trial (the pre-trial
        # audit takes an index too), so compute the next free index instead
        # of assuming it is 1.
        next_index = await reserve_next_trial_index(session, task_id=task_id)
    appended_id = f"{task_id}-{next_index}"

    holder_has_lock = asyncio.Event()
    release_holder = asyncio.Event()

    async def append_under_task_lock():
        # Mimic a sweep append: task row FOR UPDATE, then insert the next
        # free index.
        async with get_session() as session:
            await session.get(TaskModel, task_id, with_for_update=True)
            await session.execute(
                text(
                    """
                    INSERT INTO trials
                        (id, name, task_id, task_version_id, experiment_id,
                         org_id, billed_user_id, agent, provider, queue_key,
                         model, timeout_minutes, environment, harbor_config,
                         harbor_sha, is_probe, max_attempts, status, attempts,
                         created_at, updated_at)
                    SELECT :new_id, :new_id, task_id, task_version_id,
                           experiment_id, org_id, billed_user_id, agent,
                           provider, queue_key, model, timeout_minutes,
                           environment, harbor_config, harbor_sha, is_probe,
                           max_attempts, status, attempts, NOW(), NOW()
                    FROM trials WHERE id = :src_id
                    """
                ),
                {"new_id": appended_id, "src_id": f"{task_id}-0"},
            )
            holder_has_lock.set()
            await release_holder.wait()

    async def retry():
        async with get_session() as session:
            return await retry_trial_core(
                session, trial_id=f"{task_id}-0", gate_baselines=False
            )

    holder = asyncio.create_task(append_under_task_lock())
    await holder_has_lock.wait()
    retrying = asyncio.create_task(retry())
    await asyncio.sleep(1.0)
    release_holder.set()
    await holder

    # An unlocked retry computes the appended index before the append
    # commits and dies on the duplicate key; under the task lock it waits
    # and mints the index after it.
    result = await retrying
    assert result["trial_id"] == f"{task_id}-{next_index + 1}"
