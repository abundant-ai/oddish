"""The audit claim must coexist with trial foreign-key locks on its version."""

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from oddish.db import TaskModel, TaskVersionModel, VerdictStatus, get_session, init_db
from oddish.workers import analysis_trials

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
    ),
]


@pytest_asyncio.fixture
async def audit_version():
    await init_db()
    task_id = f"audit-lock-{uuid4().hex}"
    version_id = f"{task_id}-v1"
    async with get_session() as session:
        session.add(
            TaskModel(id=task_id, name=task_id, user="test", task_path="/tmp/task")
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id, task_id=task_id, version=1, task_path="/tmp/task"
            )
        )
    try:
        yield SimpleNamespace(id=task_id, name=task_id, current_version_id=version_id)
    finally:
        async with get_session() as session:
            await session.execute(
                TaskVersionModel.__table__.delete().where(
                    TaskVersionModel.id == version_id
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


async def test_concurrent_audits_do_not_upgrade_foreign_key_locks(
    audit_version, monkeypatch
):
    create = AsyncMock()
    monkeypatch.setattr(analysis_trials, "create_analysis_trial", create)
    ready = asyncio.Barrier(2)

    async def submit():
        async with get_session() as session:
            await session.execute(text("SET LOCAL lock_timeout = '2s'"))
            # The lock a trial insert takes when referencing task_versions.
            await session.scalar(
                select(TaskVersionModel.id)
                .where(TaskVersionModel.id == audit_version.current_version_id)
                .with_for_update(read=True, key_share=True)
            )
            # Keep a pre-claim version in the ORM identity map too.
            version = await session.get(
                TaskVersionModel, audit_version.current_version_id
            )
            await ready.wait()
            claimed = await analysis_trials.maybe_enqueue_audit_trial(
                session, task=audit_version, task_version_id=version.id
            )
            if claimed:
                assert version.pre_trial_status == VerdictStatus.QUEUED
            return claimed

    results = await asyncio.wait_for(asyncio.gather(submit(), submit()), timeout=5)
    assert sorted(results) == [False, True]
    create.assert_awaited_once()
    async with get_session() as session:
        version = await session.get(TaskVersionModel, audit_version.current_version_id)
        assert version.pre_trial_status == VerdictStatus.QUEUED
        assert version.pre_trial_started_at is not None


async def test_failed_audit_creation_rolls_back_claim(audit_version, monkeypatch):
    create = AsyncMock(side_effect=RuntimeError("cannot enqueue"))
    monkeypatch.setattr(analysis_trials, "create_analysis_trial", create)
    with pytest.raises(RuntimeError, match="cannot enqueue"):
        async with get_session() as session:
            await analysis_trials.maybe_enqueue_audit_trial(
                session, task=audit_version, task_version_id=None
            )
    create.side_effect = None
    async with get_session() as session:
        version = await session.get(TaskVersionModel, audit_version.current_version_id)
        assert version.pre_trial_status is None
        assert version.pre_trial_started_at is None
        assert await analysis_trials.maybe_enqueue_audit_trial(
            session, task=audit_version, task_version_id=None
        )


@pytest.mark.parametrize("status", list(VerdictStatus))
async def test_existing_audit_state_is_not_replaced(audit_version, monkeypatch, status):
    create = AsyncMock()
    monkeypatch.setattr(analysis_trials, "create_analysis_trial", create)
    async with get_session() as session:
        version = await session.get(TaskVersionModel, audit_version.current_version_id)
        version.pre_trial_status = status
    async with get_session() as session:
        assert not await analysis_trials.maybe_enqueue_audit_trial(
            session, task=audit_version, task_version_id=None
        )
        assert not await analysis_trials.maybe_enqueue_audit_trial(
            session, task=audit_version, task_version_id="absent-version"
        )
        version = await session.get(TaskVersionModel, audit_version.current_version_id)
        assert version.pre_trial_status == status
    create.assert_not_awaited()


@pytest.mark.parametrize("operation", ["audit_claim", "qa_version_lock"])
@pytest.mark.parametrize("scope", ["VERSION", "TASK"])
async def test_version_tag_projection_coexists_with_task_then_version_writes(
    audit_version, monkeypatch, operation, scope
):
    """Force the worker to contend with the task lock held by sweep/QA.

    Before the fix the worker held the version while waiting for the task;
    the request then waited for that version, forming a deadlock. Run the
    actual worker body without its retry decorator so retries cannot hide it.
    """
    from oddish.workers.queue import tag_project_handler

    monkeypatch.setattr(analysis_trials, "create_analysis_trial", AsyncMock())
    worker_pid = asyncio.get_running_loop().create_future()

    @asynccontextmanager
    async def worker_session():
        async with get_session() as session:
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            worker_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            yield session

    monkeypatch.setattr(tag_project_handler, "get_session", worker_session)
    async with get_session() as session:
        task = await session.get(TaskModel, audit_version.id)
        version = await session.get(TaskVersionModel, audit_version.current_version_id)
        task.effective_tag_ids = ["stale"]
        version.effective_tag_ids = ["stale"]

    worker = None
    try:
        async with get_session() as session:
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            request_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            await session.get(TaskModel, audit_version.id, with_for_update=True)
            # A sweep's trial inserts take this foreign-key reference lock.
            await session.scalar(
                select(TaskVersionModel.id)
                .where(TaskVersionModel.id == audit_version.current_version_id)
                .with_for_update(read=True, key_share=True)
            )
            worker = asyncio.create_task(
                tag_project_handler.run_tag_project_job.__wrapped__(
                    payload={
                        "scope": scope,
                        "target_id": (
                            audit_version.current_version_id
                            if scope == "VERSION"
                            else audit_version.id
                        ),
                        "task_id": audit_version.id,
                    }
                )
            )
            async with asyncio.timeout(3):
                pid = await worker_pid
                # Observe real blocking rather than relying on a scheduling sleep.
                while request_pid not in await session.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                ):
                    await asyncio.sleep(0.01)
            if operation == "audit_claim":
                assert await analysis_trials.maybe_enqueue_audit_trial(
                    session, task=audit_version, task_version_id=None
                )
            else:
                assert await session.get(
                    TaskVersionModel,
                    audit_version.current_version_id,
                    with_for_update=True,
                )
        summary = await asyncio.wait_for(worker, timeout=5)
        assert summary["tasks_recomputed"] == summary["versions_recomputed"] == 1
        async with get_session() as session:
            task = await session.get(TaskModel, audit_version.id)
            version = await session.get(
                TaskVersionModel, audit_version.current_version_id
            )
            assert task.effective_tag_ids == version.effective_tag_ids == []
            if operation == "audit_claim":
                assert version.pre_trial_status == VerdictStatus.QUEUED
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
