"""Cancelled audits must not leave QA waiting on a fictitious queued run."""

import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobModel,
    WorkerJobKind,
    WorkerJobStatus,
    utcnow,
)
from oddish.queue import cancel_tasks_runs
from oddish.workers.queue.cleanup import (
    _heal_cancelled_audits,
    _heal_stale_audit_imports,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
)


@pytest_asyncio.fixture
async def cancelled_audit(session):
    task_id = "cancel-audit-" + uuid.uuid4().hex[:8]
    task = TaskModel(
        id=task_id,
        name=task_id,
        user="tester",
        task_path="s3://test/task",
        status=TaskStatus.RUNNING,
        run_analysis=True,
    )
    session.add(task)
    session.add(ExperimentModel(id=task_id + "-exp", name="audit recovery"))
    await session.flush()
    version = TaskVersionModel(
        id=task_id + "-v1",
        task_id=task_id,
        version=1,
        task_s3_key="test/task",
        task_path="s3://test/task",
        pre_trial_status=VerdictStatus.QUEUED,
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    audit = TrialModel(
        id=task_id + "-audit",
        name="audit",
        experiment_id=task_id + "-exp",
        task_id=task_id,
        task_version_id=version.id,
        kind="audit",
        agent="claude-code",
        provider="anthropic",
        queue_key="anthropic/test",
        model="test",
        status=TrialStatus.FAILED,
        harbor_stage="cancelled",
        error_message="Cancelled by user",
        finished_at=utcnow(),
        created_at=utcnow() - timedelta(days=1),
    )
    session.add(audit)
    await session.flush()
    return task, version, audit


@pytest.mark.asyncio
async def test_cleanup_finalizes_cancelled_audit_without_import_or_new_jobs(
    session, cancelled_audit
):
    task, version, audit = cancelled_audit
    before = await session.scalar(select(func.count()).select_from(TrialModel))
    jobs_before = await session.scalar(select(func.count()).select_from(WorkerJobModel))
    assert audit.id not in await _heal_stale_audit_imports(session)
    assert await _heal_cancelled_audits(session) == 1
    await session.flush()
    assert version.pre_trial_status == VerdictStatus.FAILED
    assert version.pre_trial_error == "Cancelled by user"
    assert version.pre_trial_finished_at == audit.finished_at
    assert task.status == TaskStatus.FAILED
    assert task.verdict_status == VerdictStatus.FAILED
    assert task.verdict_error == "Cancelled by user"
    assert await session.scalar(select(func.count()).select_from(TrialModel)) == before
    assert (
        await session.scalar(select(func.count()).select_from(WorkerJobModel))
        == jobs_before
    )
    assert await _heal_cancelled_audits(session) == 0


@pytest.mark.asyncio
async def test_general_cancellation_updates_audit_version_in_same_transaction(
    session, cancelled_audit
):
    task, version, audit = cancelled_audit
    audit.status = TrialStatus.RUNNING
    audit.harbor_stage = "agent"
    audit.finished_at = None
    audit.error_message = None
    job = WorkerJobModel(
        kind=WorkerJobKind.TRIAL,
        status=WorkerJobStatus.RUNNING,
        subject_table="trials",
        subject_id=audit.id,
        queue_key=audit.queue_key,
    )
    session.add(job)
    await session.flush()
    result = await cancel_tasks_runs(session, [task.id])
    await session.refresh(job)
    assert result["trials_cancelled"] == 1
    assert job.status == WorkerJobStatus.CANCELLED
    assert version.pre_trial_status == VerdictStatus.FAILED
    assert version.pre_trial_error == "Cancelled by user"
    assert version.pre_trial_finished_at == audit.finished_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement_status", [TrialStatus.QUEUED, TrialStatus.SUCCESS, TrialStatus.FAILED]
)
async def test_cleanup_preserves_newer_uncancelled_audit(
    session, cancelled_audit, replacement_status
):
    task, version, audit = cancelled_audit
    replacement = TrialModel(
        id=task.id + "-replacement",
        name="replacement",
        experiment_id=audit.experiment_id,
        task_id=task.id,
        task_version_id=version.id,
        kind="audit",
        agent="claude-code",
        provider="anthropic",
        queue_key=audit.queue_key,
        model="test",
        status=replacement_status,
        created_at=utcnow(),
    )
    session.add(replacement)
    await session.flush()
    assert await _heal_cancelled_audits(session) == 0
    assert version.pre_trial_status == VerdictStatus.QUEUED
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_cleanup_waits_for_authoritative_audit_job(session, cancelled_audit):
    task, version, audit = cancelled_audit
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RETRYING,
            subject_table="trials",
            subject_id=audit.id,
            queue_key=audit.queue_key,
        )
    )
    await session.flush()
    assert await _heal_cancelled_audits(session) == 0
    assert version.pre_trial_status == VerdictStatus.QUEUED
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_cleanup_keeps_other_solver_work_running(session, cancelled_audit):
    task, version, audit = cancelled_audit
    session.add(
        TrialModel(
            id=task.id + "-solver",
            name="solver",
            experiment_id=audit.experiment_id,
            task_id=task.id,
            task_version_id=version.id,
            kind="agent",
            agent="codex",
            provider="openai",
            queue_key="openai/test",
            model="test",
            status=TrialStatus.RUNNING,
        )
    )
    await session.flush()
    assert await _heal_cancelled_audits(session) == 0
    assert version.pre_trial_status == VerdictStatus.QUEUED
    assert task.status == TaskStatus.RUNNING
    assert task.verdict_status is None


@pytest.mark.asyncio
async def test_cleanup_does_not_fail_current_task_for_an_old_version(
    session, cancelled_audit
):
    task, version, _ = cancelled_audit
    current = TaskVersionModel(
        id=task.id + "-v2",
        task_id=task.id,
        version=2,
        task_s3_key="test/v2",
        task_path="s3://test/v2",
        pre_trial_status=VerdictStatus.SUCCESS,
    )
    session.add(current)
    await session.flush()
    task.current_version_id = current.id
    await session.flush()
    assert await _heal_cancelled_audits(session) == 1
    assert version.pre_trial_status == VerdictStatus.FAILED
    assert current.pre_trial_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_cleanup_preserves_published_verdict(session, cancelled_audit):
    task, _, _ = cancelled_audit
    task.verdict_status = VerdictStatus.SUCCESS
    task.verdict = {"verdict": "ACCEPTED"}
    await session.flush()
    assert await _heal_cancelled_audits(session) == 1
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict == {"verdict": "ACCEPTED"}


@pytest.mark.asyncio
async def test_cleanup_preserves_published_audit(session, cancelled_audit):
    task, version, _ = cancelled_audit
    version.pre_trial_status = VerdictStatus.SUCCESS
    version.pre_trial = {"items": [{"title": "Existing finding"}]}
    await session.flush()
    assert await _heal_cancelled_audits(session) == 0
    assert version.pre_trial_status == VerdictStatus.SUCCESS
    assert version.pre_trial == {"items": [{"title": "Existing finding"}]}
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_cleanup_preserves_task_with_active_job_despite_terminal_trial(
    session, cancelled_audit
):
    task, version, audit = cancelled_audit
    solver = TrialModel(
        id=task.id + "-solver",
        name="solver",
        task_id=task.id,
        task_version_id=version.id,
        experiment_id=audit.experiment_id,
        kind="agent",
        agent="codex",
        provider="openai",
        queue_key="openai/test",
        model="test",
        status=TrialStatus.FAILED,
    )
    session.add(solver)
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RETRYING,
            subject_table="trials",
            subject_id=solver.id,
            queue_key=solver.queue_key,
        )
    )
    await session.flush()
    assert await _heal_cancelled_audits(session) == 0
    assert version.pre_trial_status == VerdictStatus.QUEUED
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_scoped_cancellation_preserves_other_experiments_audit(
    session, cancelled_audit
):
    task, version, audit = cancelled_audit
    audit.status = TrialStatus.RUNNING
    audit.harbor_stage = "agent"
    other_exp = ExperimentModel(id=task.id + "-other", name="other experiment")
    session.add(other_exp)
    await session.flush()
    replacement = TrialModel(
        id=task.id + "-replacement",
        name="replacement",
        task_id=task.id,
        task_version_id=version.id,
        experiment_id=other_exp.id,
        kind="audit",
        agent="claude-code",
        provider="anthropic",
        queue_key=audit.queue_key,
        model="test",
        status=TrialStatus.RUNNING,
        created_at=utcnow(),
    )
    session.add(replacement)
    await session.flush()
    result = await cancel_tasks_runs(
        session, [task.id], experiment_id=audit.experiment_id
    )
    assert result["trials_cancelled"] == 1
    assert audit.status == TrialStatus.FAILED
    assert replacement.status == TrialStatus.RUNNING
    assert version.pre_trial_status == VerdictStatus.QUEUED
    assert task.status == TaskStatus.RUNNING
