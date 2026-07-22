import uuid

import pytest

from oddish.analyze.models import ActionItem, ActionItemSource, ActionTier, Dimension, ProblemType
from oddish.core.verdict_sync import build_pre_trial_payload, sync_pre_trial_to_task_version
from oddish.db import get_session
from oddish.db.models import JobStatus, TaskModel, TaskStatus, TaskVersionModel
from oddish.workers.queue.qa_handler import _claim_pre_trial_version


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


async def _make_task_with_version(*, with_current_version: bool = True) -> tuple[str, str]:
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    version_id = f"{task_id}-v1"
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
        # The tasks<->task_versions FK cycle (current_version_id / task_id)
        # makes UOW insert ordering nondeterministic; flush the task first.
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="/tmp/does-not-matter",
            )
        )
        await session.commit()
    if with_current_version:
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            task.current_version_id = version_id
            await session.commit()
    return task_id, version_id


async def _cleanup(task_id: str) -> None:
    async with get_session() as session:
        # task_versions rows cascade off the task delete.
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.commit()


def test_payload_assigns_ids():
    payload = build_pre_trial_payload([_item()])
    assert payload["items"][0]["id"]  # computed
    assert payload["items"][0]["dimension"] == "oracle"


@pytest.mark.asyncio
async def test_sync_writes_version_columns_without_completing_task():
    task_id, version_id = await _make_task_with_version()
    try:
        await sync_pre_trial_to_task_version(
            version_id, payload=build_pre_trial_payload([_item()]), error=None
        )
        async with get_session() as session:
            version = await session.get(TaskVersionModel, version_id)
            task = await session.get(TaskModel, task_id)
            assert version.pre_trial_status == JobStatus.SUCCESS
            assert version.pre_trial["items"][0]["file"] == "solution.py"
            # pre-trial must not complete the task or touch its verdict
            assert task.status != TaskStatus.COMPLETED
            assert task.verdict_status is None
    finally:
        await _cleanup(task_id)


@pytest.mark.asyncio
async def test_sync_records_failure_without_touching_verdict():
    task_id, version_id = await _make_task_with_version()
    try:
        await sync_pre_trial_to_task_version(
            version_id, payload=None, error=RuntimeError("boom")
        )
        async with get_session() as session:
            version = await session.get(TaskVersionModel, version_id)
            task = await session.get(TaskModel, task_id)
            assert version.pre_trial_status == JobStatus.FAILED
            assert "boom" in version.pre_trial_error
            assert task.verdict_status is None
            assert task.status != TaskStatus.COMPLETED
    finally:
        await _cleanup(task_id)


@pytest.mark.asyncio
async def test_claim_marks_current_version_running():
    task_id, version_id = await _make_task_with_version()
    try:
        claimed = await _claim_pre_trial_version(task_id)
        assert claimed == version_id
        async with get_session() as session:
            version = await session.get(TaskVersionModel, version_id)
            assert version.pre_trial_status == JobStatus.RUNNING
            assert version.pre_trial_started_at is not None
    finally:
        await _cleanup(task_id)


@pytest.mark.asyncio
async def test_claim_skips_task_without_current_version():
    task_id, _ = await _make_task_with_version(with_current_version=False)
    try:
        assert await _claim_pre_trial_version(task_id) is None
    finally:
        await _cleanup(task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [JobStatus.SUCCESS, JobStatus.FAILED])
async def test_claim_skips_already_audited_version(terminal):
    # Once per task version: a sweep-append QA re-run must not re-audit
    # unchanged source.
    task_id, version_id = await _make_task_with_version()
    try:
        async with get_session() as session:
            version = await session.get(TaskVersionModel, version_id)
            version.pre_trial_status = terminal
            await session.commit()
        assert await _claim_pre_trial_version(task_id) is None
    finally:
        await _cleanup(task_id)


@pytest.mark.asyncio
async def test_claim_reclaims_stale_running_version():
    # A worker that died mid-audit leaves RUNNING; the next QA run must be
    # able to reclaim rather than wedge the version forever.
    task_id, version_id = await _make_task_with_version()
    try:
        async with get_session() as session:
            version = await session.get(TaskVersionModel, version_id)
            version.pre_trial_status = JobStatus.RUNNING
            await session.commit()
        assert await _claim_pre_trial_version(task_id) == version_id
    finally:
        await _cleanup(task_id)
