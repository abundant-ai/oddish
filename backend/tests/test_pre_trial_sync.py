import uuid

import pytest

from oddish.analyze.models import ActionItem, ActionItemSource, ActionTier, Dimension, ProblemType
from oddish.core.verdict_sync import build_pre_trial_payload, sync_pre_trial_to_task
from oddish.db import get_session
from oddish.db.models import JobStatus, TaskModel, TaskStatus


def _item():
    return ActionItem(
        source=ActionItemSource.PRE_TRIAL,
        problem_type=ProblemType.MISMATCH,
        dimension=Dimension.ORACLE,
        file="solution.py",
        line_start=1,
        line_end=1,
        title="t",
        detail="d",
        recommendation="r",
        tier=ActionTier.SHOULD_FIX,
    )


def test_payload_assigns_ids():
    payload = build_pre_trial_payload([_item()])
    assert payload["items"][0]["id"]  # computed
    assert payload["items"][0]["dimension"] == "oracle"


@pytest.mark.asyncio
async def test_sync_writes_columns_without_completing_task():
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            TaskModel(
                id=task_id,
                name=f"pre-trial-sync-{task_id}",
                user="test",
                task_path="/tmp/does-not-matter",
                status=TaskStatus.RUNNING,
            )
        )
        await session.commit()
    try:
        await sync_pre_trial_to_task(
            task_id, payload=build_pre_trial_payload([_item()]), error=None
        )
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            assert task.pre_trial_status == JobStatus.SUCCESS
            assert task.pre_trial["items"][0]["file"] == "solution.py"
            assert task.status != TaskStatus.COMPLETED  # pre-trial must not complete the task
    finally:
        async with get_session() as session:
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
            await session.commit()


@pytest.mark.asyncio
async def test_sync_records_failure_without_touching_verdict():
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            TaskModel(
                id=task_id,
                name=f"pre-trial-sync-fail-{task_id}",
                user="test",
                task_path="/tmp/does-not-matter",
                status=TaskStatus.RUNNING,
            )
        )
        await session.commit()
    try:
        await sync_pre_trial_to_task(task_id, payload=None, error=RuntimeError("boom"))
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            assert task.pre_trial_status == JobStatus.FAILED
            assert "boom" in task.pre_trial_error
            assert task.verdict_status is None
            assert task.status != TaskStatus.COMPLETED
    finally:
        async with get_session() as session:
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
            await session.commit()
