"""Exercise retry repair and failure atomicity against an isolated Postgres DB."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from oddish.core.dashboard import get_worker_job_usage_core
from oddish.core.admin import get_orphaned_state_core
from oddish.core.helpers import fetch_trial_queue_info
from oddish.core.retry_reconciliation import reconcile_terminal_retry_trials
from oddish.db import (
    ExperimentModel,
    TaskVersionModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    WorkerJobKind,
    WorkerJobStatus,
)
from oddish.db.connection import get_session
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.queue import worker_job_single_job as runner

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ODDISH_DATABASE_URL"),
        reason="Requires an isolated Postgres DB",
    ),
]


@pytest_asyncio.fixture
async def cohort(monkeypatch):
    suffix = uuid4().hex[:8]
    task_id = f"retry-test-{suffix}"
    org_id = f"org-{suffix}"
    past = datetime.now(timezone.utc) - timedelta(days=3)
    async with get_session() as session:
        session.add(ExperimentModel(id=task_id, name=task_id, org_id=org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="tester",
                org_id=org_id,
                task_path="s3://test/task",
            )
        )

    async with get_session() as session:
        session.add(
            TaskVersionModel(
                id=task_id + "-v1",
                task_id=task_id,
                version=1,
                task_path="s3://test/task",
            )
        )

    async def seed(
        name,
        job_status=None,
        *,
        trial_status=TrialStatus.RETRYING,
        attempts=6,
        max_attempts=6,
        recent=False,
    ):
        tid = f"{task_id}-{name}"
        when = datetime.now(timezone.utc) if recent else past
        async with get_session() as session:
            trial = TrialModel(
                id=tid,
                name=tid,
                task_id=task_id,
                org_id=org_id,
                experiment_id=task_id,
                task_version_id=task_id + "-v1",
                agent="codex",
                model="openai/test",
                provider="openai",
                queue_key="openai/test",
                status=trial_status,
                attempts=5,
                max_attempts=6,
                created_at=when,
                updated_at=when,
                error_message="previous failure",
            )
            session.add(trial)
            if job_status:
                session.add(
                    WorkerJobModel(
                        id=f"job-{suffix}-{name}",
                        kind=WorkerJobKind.TRIAL,
                        status=job_status,
                        queue_key="openai/test",
                        org_id=org_id,
                        subject_table="trials",
                        subject_id=tid,
                        attempts=attempts,
                        max_attempts=max_attempts,
                        current_worker_id="test-worker",
                        created_at=when,
                        available_after=when,
                        finished_at=(
                            when
                            if job_status
                            in (
                                WorkerJobStatus.FAILED,
                                WorkerJobStatus.CANCELLED,
                                WorkerJobStatus.SUCCESS,
                            )
                            else None
                        ),
                        error_message=(
                            "Cancelled by user"
                            if job_status == WorkerJobStatus.CANCELLED
                            else "last failure"
                        ),
                    )
                )
        return tid, f"job-{suffix}-{name}"

    async def no_hooks(_trial_id):
        pass

    monkeypatch.setattr(
        "oddish.workers.queue.trial_handler._run_post_trial_hooks", no_hooks
    )
    yield seed, org_id, task_id
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM worker_jobs WHERE org_id = :org"), {"org": org_id}
        )
        await session.execute(
            text("DELETE FROM trials WHERE task_id = :id"), {"id": task_id}
        )
        await session.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
        await session.execute(
            text("DELETE FROM experiments WHERE id = :id"), {"id": task_id}
        )


async def outcome(tid, jid):
    return await runner._record_outcome(
        job_id=jid,
        worker_id="test-worker",
        outcome=JobOutcome.fail("final failure"),
        attempts=5,
        max_attempts=6,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id=tid,
    )


async def test_exhausted_worker_updates_stale_trial_attempt_count(cohort):
    seed, _, _ = cohort
    tid, jid = await seed("failed", WorkerJobStatus.RUNNING)
    assert await outcome(tid, jid) == WorkerJobStatus.FAILED
    async with get_session() as session:
        trial = await session.get(TrialModel, tid)
        job = await session.get(WorkerJobModel, jid)
        assert trial.status == TrialStatus.FAILED
        assert trial.attempts == job.attempts == 6
        assert trial.error_message == "final failure"
        assert trial.next_retry_at is None


async def test_cancelled_job_cannot_be_resurrected_by_late_outcome(cohort):
    seed, _, _ = cohort
    tid, jid = await seed(
        "cancel", WorkerJobStatus.CANCELLED, trial_status=TrialStatus.FAILED
    )
    assert await outcome(tid, jid) is None
    async with get_session() as session:
        assert (await session.get(TrialModel, tid)).error_message == "previous failure"
        assert (
            await session.get(WorkerJobModel, jid)
        ).status == WorkerJobStatus.CANCELLED


async def test_trial_write_failure_rolls_back_worker_retry(cohort):
    seed, _, task_id = cohort
    tid, jid = await seed(
        "atomic", WorkerJobStatus.RUNNING, trial_status=TrialStatus.RUNNING, attempts=2
    )
    constraint = task_id.replace("-", "_")
    async with get_session() as session:
        await session.execute(
            text(
                f"ALTER TABLE trials ADD CONSTRAINT {constraint} "
                f"CHECK (id <> '{tid}' OR status <> 'RETRYING') NOT VALID"
            )
        )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await outcome(tid, jid)
        async with get_session() as session:
            job = await session.get(WorkerJobModel, jid)
            assert job.status == WorkerJobStatus.RUNNING
            assert job.next_retry_at is None
    finally:
        async with get_session() as session:
            await session.execute(
                text(f"ALTER TABLE trials DROP CONSTRAINT {constraint}")
            )


async def test_reconciliation_dry_run_is_scoped_and_repeatable(cohort):
    seed, org_id, _ = cohort
    failed, _ = await seed("failed", WorkerJobStatus.FAILED)
    cancelled, _ = await seed("cancelled", WorkerJobStatus.CANCELLED)
    active, _ = await seed("active", WorkerJobStatus.RETRYING)
    missing, _ = await seed("missing")
    success, _ = await seed("success", WorkerJobStatus.SUCCESS)
    recent, _ = await seed("recent", WorkerJobStatus.FAILED, recent=True)
    async with get_session() as session:
        assert (
            await reconcile_terminal_retry_trials(session, org_id="another-org") == []
        )
        changes = await reconcile_terminal_retry_trials(session, org_id=org_id)
        assert {x["id"] for x in changes} == {failed, cancelled}
        assert (await session.get(TrialModel, failed)).status == TrialStatus.RETRYING
    async with get_session() as session:
        changes = await reconcile_terminal_retry_trials(
            session, org_id=org_id, dry_run=False
        )
        assert {x["id"] for x in changes} == {failed, cancelled}
    async with get_session() as session:
        assert (
            await reconcile_terminal_retry_trials(session, org_id=org_id, dry_run=False)
            == []
        )
        assert (
            await session.get(TrialModel, cancelled)
        ).error_message == "Cancelled by user"
        assert (await session.get(TrialModel, cancelled)).harbor_stage == "cancelled"
        for tid in [active, missing, success, recent]:
            assert (await session.get(TrialModel, tid)).status == TrialStatus.RETRYING


async def test_reconciliation_skips_concurrent_task_edit(cohort):
    seed, org_id, task_id = cohort
    await seed("locked", WorkerJobStatus.FAILED)
    async with get_session() as lock_session:
        await lock_session.execute(
            select(TaskModel.id).where(TaskModel.id == task_id).with_for_update()
        )
        async with get_session() as session:
            assert (
                await reconcile_terminal_retry_trials(
                    session, org_id=org_id, dry_run=False
                )
                == []
            )
    async with get_session() as session:
        assert (
            len(
                await reconcile_terminal_retry_trials(
                    session, org_id=org_id, dry_run=False
                )
            )
            == 1
        )


async def test_old_active_jobs_count_outside_cost_window_and_stale_trials_have_no_position(
    cohort,
):
    seed, org_id, _ = cohort
    active, _ = await seed("active", WorkerJobStatus.RETRYING)
    missing, _ = await seed("missing")
    await seed("finished", WorkerJobStatus.FAILED)
    async with get_session() as session:
        rows = await get_worker_job_usage_core(session, org_id=org_id, usage_minutes=60)
        assert len(rows) == 1
        assert rows[0]["retrying"] == 1
        assert rows[0]["job_count"] == rows[0]["failed"] == 0
        trials = [(await session.get(TrialModel, tid)) for tid in [active, missing]]
        positions = await fetch_trial_queue_info(session, trials=trials)
        assert set(positions) == {active}
        assert positions[active].position == 1


async def test_reconciliation_never_overrides_another_active_job(cohort):
    seed, org_id, _ = cohort
    tid, jid = await seed("history", WorkerJobStatus.FAILED)
    async with get_session() as session:
        session.add(
            WorkerJobModel(
                id=jid + "-active",
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.QUEUED,
                subject_table="trials",
                subject_id=tid,
                queue_key="openai/test",
                org_id=org_id,
                created_at=datetime.now(timezone.utc) - timedelta(days=4),
            )
        )
    async with get_session() as session:
        assert (
            await reconcile_terminal_retry_trials(session, org_id=org_id, dry_run=False)
            == []
        )


async def test_orphan_diagnostics_expose_retries_with_no_active_job(cohort):
    seed, org_id, _ = cohort
    missing, _ = await seed("missing")
    await seed("active", WorkerJobStatus.RETRYING)
    async with get_session() as session:
        response = await get_orphaned_state_core(session, org_id=org_id)
        assert response.counts.retrying_without_worker == 1
        assert [sample.trial_id for sample in response.trial_samples] == [missing]
        assert response.trial_samples[0].issue == "retrying_without_worker"
