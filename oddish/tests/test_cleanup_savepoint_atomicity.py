"""Each stale-heartbeat job's domain mirror is atomic within its own savepoint.

Invariant: a later job raising ``_DomainRowLocked`` (its domain row is locked by
a concurrent settle/retry) must NOT revert an EARLIER job's already-committed
mirror. Today SQLAlchemy's ``begin_nested`` flushes pending ORM state safely
before opening the next savepoint, and the per-job ``session.flush()`` makes the
atomicity explicit rather than relying on that timing -- this test locks the
invariant down so neither a SQLAlchemy version bump nor a refactor of the sweep
can silently reintroduce cross-job bleed.

Faithful setup: real Postgres SAVEPOINTs + a second connection holding a
``FOR UPDATE`` lock on the later job's trial (so the reaper's SKIP LOCKED probe
raises ``_DomainRowLocked`` for it). The earlier job's mirror must survive.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

import oddish.db.connection as _conn
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.workers.queue import cleanup

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


@requires_db
@pytest.mark.asyncio
async def test_prior_job_mirror_survives_later_domain_lock(monkeypatch):
    sfx = uuid.uuid4().hex[:8]
    org_id = f"org-rz-{sfx}"
    exp_id = f"exp-rz-{sfx}"
    task_id = f"task-rz-{sfx}"
    trial_a = f"{task_id}-0"
    trial_b = f"{task_id}-1"
    # "zzz"-prefixed + random suffix: A/B sort adjacent and after any pre-existing
    # worker_jobs, so B is the iteration IMMEDIATELY after A regardless of other
    # DB rows -- the exact condition under which the pre-fix bug reverted A.
    job_a = f"zzza_{sfx}"
    job_b = f"zzzb_{sfx}"

    async with _conn.async_session_maker() as s:
        s.add(ExperimentModel(id=exp_id, name=exp_id, org_id=org_id))
        s.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
                user="tester",
                task_path="s3://test-bucket/reaper-atomicity",
                status=TaskStatus.RUNNING,
            )
        )
        for tid in (trial_a, trial_b):
            s.add(
                TrialModel(
                    id=tid,
                    name=tid,
                    task_id=task_id,
                    experiment_id=exp_id,
                    org_id=org_id,
                    agent="codex",
                    provider="openai",
                    queue_key="openai/gpt-5",
                    model="gpt-5",
                    is_probe=False,
                    max_attempts=6,
                    attempts=1,
                    status=TrialStatus.RUNNING,
                )
            )
        for jid, tid in ((job_a, trial_a), (job_b, trial_b)):
            s.add(
                WorkerJobModel(
                    id=jid,
                    kind=WorkerJobKind.TRIAL,
                    status=WorkerJobStatus.RUNNING,
                    queue_key="openai/gpt-5",
                    subject_table="trials",
                    subject_id=tid,
                    attempts=1,
                    max_attempts=6,
                    # heartbeat_at left NULL -> qualifies as stale immediately.
                )
            )
        await s.commit()

    async def _no_stage(_session, _trial_id):
        return False

    async def _no_zombie():
        return 0

    async def _no_tag_reconcile(_session):
        return 0

    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", _no_stage)
    monkeypatch.setattr("oddish.queue.maybe_advance_legacy_analyzing_task", _no_stage)
    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", _no_zombie)
    # Later tag-reconciliation steps are unrelated to the step-1 atomicity under
    # test and depend on tables that may lag in a local dev DB; neutralize them
    # so an unrelated failure can't roll back the sweep we're asserting on.
    monkeypatch.setattr(cleanup, "_maybe_reconcile_tag_projections", _no_tag_reconcile)
    monkeypatch.setattr(cleanup, "sweep_orphaned_tag_owners", _no_tag_reconcile)

    # Hold a FOR UPDATE lock on trial B in a separate connection for the whole
    # sweep, so the reaper's FOR UPDATE SKIP LOCKED probe raises _DomainRowLocked.
    lock_conn = _conn.async_session_maker()
    await lock_conn.__aenter__()
    try:
        await lock_conn.execute(
            text("SELECT id FROM trials WHERE id = :id FOR UPDATE"), {"id": trial_b}
        )
        await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)
    finally:
        await lock_conn.rollback()
        await lock_conn.close()

    try:
        async with _conn.async_session_maker() as s:
            trial_a_row = await s.get(TrialModel, trial_a)
            trial_b_row = await s.get(TrialModel, trial_b)
            job_a_status = (
                await s.execute(
                    text("SELECT status::text FROM worker_jobs WHERE id = :id"),
                    {"id": job_a},
                )
            ).scalar_one()
            job_b_status = (
                await s.execute(
                    text("SELECT status::text FROM worker_jobs WHERE id = :id"),
                    {"id": job_b},
                )
            ).scalar_one()

            # A: job reaped to RETRYING AND its trial mirror survived the later
            # job's savepoint rollback (the whole point of the fix).
            assert job_a_status == "RETRYING"
            assert trial_a_row.status == TrialStatus.RETRYING
            assert trial_a_row.current_worker_id is None
            assert trial_a_row.next_retry_at is not None

            # B: domain row was locked -> the job's savepoint rolled back, so the
            # job stays RUNNING and its trial is untouched (retried next sweep).
            assert job_b_status == "RUNNING"
            assert trial_b_row.status == TrialStatus.RUNNING
    finally:
        async with _conn.async_session_maker() as s:
            await s.execute(
                text("DELETE FROM worker_jobs WHERE id = ANY(:ids)"),
                {"ids": [job_a, job_b]},
            )
            await s.execute(
                text("DELETE FROM trials WHERE task_id = :t"), {"t": task_id}
            )
            await s.execute(text("DELETE FROM tasks WHERE id = :t"), {"t": task_id})
            await s.execute(
                text("DELETE FROM experiments WHERE id = :e"), {"e": exp_id}
            )
            await s.commit()
