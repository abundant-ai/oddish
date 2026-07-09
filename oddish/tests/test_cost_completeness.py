from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
from oddish.core import endpoints  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
)
from oddish.workers.queue import cleanup  # noqa: E402


def _billable_trial(**overrides):
    default_trial_fields = dict(
        id="trial-1",
        name="trial-1",
        task_id="task-1",
        task_version_id="task-1-v1",
        experiment_id="exp-1",
        org_id="org-1",
        billed_user_id=None,
        agent="codex",
        model="gpt-5",
        provider="openai",
        queue_key="openai/gpt-5",
        timeout_minutes=None,
        environment=None,
        is_probe=False,
        harbor_config=None,
        status=TrialStatus.RUNNING,
        error_message=None,
        harbor_stage=None,
        finished_at=None,
        started_at=None,
        max_attempts=6,
        attempts=1,
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        total_steps=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="w-1",
        current_queue_slot=0,
        heartbeat_at=None,
        stale_reaped_at=None,
        next_retry_at=None,
        superseded_by_trial_id=None,
        deleted_at=None,
        result=None,
        idempotency_key="idem-1",
        claimed_at=None,
        analysis=None,
        analysis_status=None,
        analysis_error=None,
        analysis_started_at=None,
        analysis_finished_at=None,
    )
    default_trial_fields.update(overrides)
    return SimpleNamespace(**default_trial_fields)


async def _seed_retryable_trial(
    session, *, org_id, status, cost_usd, error_message, billed_user_id=None
):
    """Insert a real org/experiment/task/trial tree for a retry test.

    retry_trial_core does session.get(TrialModel, ...) + a raw-SQL CAS UPDATE
    that a SimpleNamespace can't satisfy, so it is exercised against live rows.
    Returns (task_id, old_trial_id, experiment_id).
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp-{suffix}"
    task_id = f"task-{suffix}"
    old_trial_id = f"{task_id}-0"

    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/retry-fake-task",
            status=TaskStatus.RUNNING,
        )
    )
    session.add(
        TrialModel(
            id=old_trial_id,
            name=old_trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id=org_id,
            billed_user_id=billed_user_id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            model="gpt-5",
            is_probe=False,
            max_attempts=6,
            attempts=1,
            status=status,
            error_message=error_message,
            cost_usd=cost_usd,
        )
    )
    await session.flush()
    await session.commit()
    return task_id, old_trial_id, experiment_id


@pytest.mark.asyncio
async def test_classifier_skips_deleted_trial(monkeypatch):
    """The analysis claim must not burn LLM spend on a tombstoned trial."""
    import oddish.workers.queue.analysis_handler as analysis_handler

    deleted_trial = _billable_trial(deleted_at="2026-07-02", analysis_status=None)

    @asynccontextmanager
    async def fake_trial_session(
        trial_id, *, allow_missing=False, with_for_update=False
    ):
        yield object(), deleted_trial

    monkeypatch.setattr(analysis_handler, "_trial_session", fake_trial_session)

    assert await analysis_handler.classify_trial_and_store("trial-1") is None
    assert deleted_trial.analysis_status is None  # never marked RUNNING


# --- M3: retry x deletion races must not strand an unclaimable reservation -----


@pytest.mark.asyncio
async def test_retry_races_trial_deletion_and_409s(monkeypatch, session):
    from fastapi import HTTPException
    from sqlalchemy import text as sa_text

    task_id, old_trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id="org-1",
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message="stuck",
    )

    async def reserve_and_race_delete(_session, *, task_id):
        # Simulate a delete winning between the retry's load and its CAS.
        await session.execute(
            sa_text("UPDATE trials SET deleted_at = NOW() WHERE id = :id"),
            {"id": old_trial_id},
        )
        return 1

    async def fail_if_enqueued(_session, **kwargs):
        raise AssertionError("must not enqueue after losing the delete race")

    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", reserve_and_race_delete)
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fail_if_enqueued)

    try:
        with pytest.raises(HTTPException) as raised:
            await endpoints.retry_trial_core(
                session, trial_id=old_trial_id, org_id="org-1"
            )
        assert raised.value.status_code == 409
    finally:
        await session.rollback()
        await session.execute(
            sa_text("DELETE FROM tasks WHERE id = :id"), {"id": task_id}
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_retry_races_task_deletion_and_409s(monkeypatch, session):
    from fastapi import HTTPException
    from sqlalchemy import text as sa_text

    task_id, old_trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id="org-1",
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message="stuck",
    )

    async def reserve_and_race_task_delete(_session, *, task_id):
        await session.execute(
            sa_text("UPDATE tasks SET deleted_at = NOW() WHERE id = :id"),
            {"id": task_id},
        )
        return 1

    async def fail_if_enqueued(_session, **kwargs):
        raise AssertionError("must not enqueue a trial under a deleted task")

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", reserve_and_race_task_delete
    )
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fail_if_enqueued)

    try:
        with pytest.raises(HTTPException) as raised:
            await endpoints.retry_trial_core(
                session, trial_id=old_trial_id, org_id="org-1"
            )
        assert raised.value.status_code == 409
    finally:
        await session.rollback()
        await session.execute(
            sa_text("DELETE FROM tasks WHERE id = :id"), {"id": task_id}
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


# --- experiment deletion: tasks alive via other experiments keep their jobs ----


@pytest.mark.asyncio
async def test_experiment_delete_keeps_jobs_of_tasks_alive_via_other_experiments(
    monkeypatch, session
):
    """B3: a task linked to E1 AND E2 (with live E2 trials) must keep its
    task-level QA/VERDICT jobs when E1 is deleted -- only dying tasks lose them."""
    from sqlalchemy import text as sa_text

    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-b3-{suffix}"
    task_id, e1_trial_id, e1_id = await _seed_retryable_trial(
        session,
        org_id=org_id,
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message=None,
    )
    e2_id = f"exp-b3b-{suffix}"
    session.add(ExperimentModel(id=e2_id, name=e2_id, org_id=org_id))
    e2_trial_id = f"{task_id}-1"
    session.add(
        TrialModel(
            id=e2_trial_id,
            name=e2_trial_id,
            task_id=task_id,
            experiment_id=e2_id,
            org_id=org_id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            model="gpt-5",
            is_probe=False,
            max_attempts=6,
            attempts=0,
            status=TrialStatus.QUEUED,
        )
    )
    await session.flush()
    for eid in (e1_id, e2_id):
        await session.execute(
            sa_text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:t, :e, NOW()) ON CONFLICT DO NOTHING"
            ),
            {"t": task_id, "e": eid},
        )
    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus

    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.QA,
            status=WorkerJobStatus.RUNNING,
            queue_key="qa",
            subject_table="tasks",
            subject_id=task_id,
        )
    )
    await session.commit()

    try:
        await endpoints.delete_experiment_core(
            session, experiment_id=e1_id, org_id=org_id
        )

        job_status = await session.scalar(
            sa_text(
                "SELECT status::text FROM worker_jobs "
                "WHERE subject_table = 'tasks' AND subject_id = :tid"
            ),
            {"tid": task_id},
        )
        assert job_status == "RUNNING"  # task survives via E2 -> job untouched
        task_deleted_at = await session.scalar(
            sa_text("SELECT deleted_at FROM tasks WHERE id = :tid"), {"tid": task_id}
        )
        assert task_deleted_at is None
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id == task_id
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_([e1_id, e2_id])
            )
        )
        await session.commit()


# --- reaper skip-locked: never wait on a trial row while holding worker_jobs ---


@pytest.mark.asyncio
async def test_reaper_skips_trial_row_locked_by_another_session(monkeypatch, session):
    import oddish.db.connection as _conn
    from sqlalchemy import select as sa_select
    from sqlalchemy import text as sa_text

    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus, utcnow

    await session.execute(
        sa_text(
            "CREATE TABLE IF NOT EXISTS tag_projection_sweep_state ("
            "id BOOLEAN PRIMARY KEY DEFAULT TRUE, "
            "last_full_sweep_at TIMESTAMPTZ, "
            "CONSTRAINT tag_sweep_singleton CHECK (id))"
        )
    )
    await session.commit()

    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-reap-{suffix}"
    task_id, trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id=org_id,
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message=None,
    )
    from datetime import timedelta

    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RUNNING,
            queue_key="openai/gpt-5",
            subject_table="trials",
            subject_id=trial_id,
            attempts=6,
            max_attempts=6,
            heartbeat_at=utcnow() - timedelta(hours=1),
        )
    )
    await session.commit()

    async def no_zombie_reap():
        return 0

    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", no_zombie_reap)

    try:
        async with _conn.async_session_maker() as locker:
            await locker.execute(
                sa_select(TrialModel).where(TrialModel.id == trial_id).with_for_update()
            )
            # The reaper CASes the stale worker_jobs row, then must SKIP the
            # locked trial row instead of blocking on it (deadlock risk).
            await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)
            await locker.rollback()

        untouched = await session.get(TrialModel, trial_id)
        await session.refresh(untouched)
        assert untouched.status == TrialStatus.RUNNING
        assert untouched.cost_usd is None
        assert untouched.stale_reaped_at is None

        # R2: the job's CAS rolled back with the skipped mirror (per-job
        # savepoint), so the whole unit is retried next sweep -- no terminal
        # job stranded next to a live trial.
        job_status = await session.scalar(
            sa_text("SELECT status::text FROM worker_jobs WHERE subject_id = :tid"),
            {"tid": trial_id},
        )
        assert job_status == "RUNNING"
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id == trial_id
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()
