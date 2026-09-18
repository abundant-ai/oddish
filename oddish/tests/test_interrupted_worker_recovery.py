"""Real PostgreSQL ownership races and durable retry fencing."""

import asyncio
import importlib.util
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from test_queue_launch_reservations import database, seed_resource_trials  # noqa: F401
from oddish.costs.recorder import WorkerBillingSpec
from oddish.workers.queue import worker_job_single_job as runner, cleanup, slots


@pytest_asyncio.fixture
async def claimed(database, monkeypatch):  # noqa: F811
    import oddish.db

    schema = await database.fetchval("SELECT current_schema()")
    engine = create_async_engine(
        os.environ["ODDISH_TEST_DATABASE_URL"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with maker() as s:
            async with s.begin():
                yield s

    monkeypatch.setattr(oddish.db, "get_session", session)
    monkeypatch.setattr(cleanup, "get_session", session)
    migration = (
        Path(__file__).parents[1] / "alembic/versions/worker_interruptions_001.py"
    )
    spec = importlib.util.spec_from_file_location("interrupt_migration", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await database.execute(module.ATTEMPT_OUTCOME_SQL)
    await seed_resource_trials(database, count=1)
    slot = await slots.acquire_queue_slot(
        queue_key="m", limit=1, worker_id="old", lease_seconds=3600
    )
    job = await runner.claim_single_worker_job(
        "m",
        worker_id="old",
        queue_slot=slot,
        modal_function_call_id="fc-shared",
        worker_billing_spec=WorkerBillingSpec(
            1, 3072, True, modal_container_id="ta-old", reservation_token="launch-old"
        ),
    )
    await database.execute(
        "UPDATE trials SET status='RUNNING', current_worker_id='old', attempts=1 WHERE id='trial-1'"
    )
    yield database, job
    await engine.dispose()


async def recover(job, **kwargs):
    return await runner.settle_interrupted_worker(
        job_id=job.id,
        attempt=job.attempts,
        worker_id="old",
        container_id=kwargs.get("container", "ta-old"),
        stopped_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_competing_replacements_one_retry_and_cleanup_gate(claimed, monkeypatch):
    db, job = claimed
    await db.execute(
        "UPDATE worker_jobs SET provider='daytona', external_id='sandbox-old' WHERE id=$1",
        job.id,
    )
    assert sum(await asyncio.gather(recover(job), recover(job))) == 1
    row = await db.fetchrow("SELECT * FROM worker_resource_attempts")
    assert row["outcome"] == "INTERRUPTED" and row["cleanup_pending"]
    assert row["modal_container_id"] == "ta-old"
    from oddish.workers.queue.worker_job_dispatcher import (
        get_worker_job_org_queue_counts,
    )

    await db.execute("UPDATE worker_jobs SET available_after=NOW() WHERE id=$1", job.id)
    demand, _ = await get_worker_job_org_queue_counts(["m"])
    assert demand == {}
    assert (
        await runner.claim_single_worker_job("m", worker_id="new", queue_slot=0) is None
    )

    async def failed(*args):
        return False

    monkeypatch.setattr(cleanup, "cancel_job_by_worker", failed)
    assert await cleanup.finish_interrupted_attempt_cleanup() == 0
    assert await db.fetchval("SELECT cleanup_pending FROM worker_resource_attempts")
    seen = []

    async def success(provider, handle):
        seen.append((provider, handle))
        return True

    monkeypatch.setattr(cleanup, "cancel_job_by_worker", success)
    assert await cleanup.finish_interrupted_attempt_cleanup() == 1
    assert seen == [("daytona", "sandbox-old")]
    await db.execute("UPDATE worker_jobs SET available_after=NOW() WHERE id=$1", job.id)
    slot = await slots.acquire_queue_slot(
        queue_key="m", limit=1, worker_id="new", lease_seconds=3600
    )
    assert slot == 0
    assert (
        await slots.acquire_queue_slot(
            queue_key="m", limit=1, worker_id="duplicate", lease_seconds=3600
        )
        is None
    )
    new = await runner.claim_single_worker_job(
        "m",
        worker_id="new",
        queue_slot=slot,
        worker_billing_spec=WorkerBillingSpec(
            1, 3072, True, modal_container_id="ta-new"
        ),
    )
    assert new.attempts == 2
    assert not await recover(job)
    await runner._record_outcome(
        job_id=new.id,
        worker_id="new",
        outcome=runner.JobOutcome.ok(),
        attempts=new.attempts,
        max_attempts=new.max_attempts,
    )
    outcomes = await db.fetch(
        "SELECT outcome FROM worker_resource_attempts ORDER BY attempt"
    )
    assert [r["outcome"] for r in outcomes] == ["INTERRUPTED", "SUCCESS"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["job_cancelled", "trial_cancelled", "exhausted", "wrong_container", "new_owner"],
)
async def test_ownership_cancellation_and_retry_allowance(claimed, mode):
    db, job = claimed
    if mode == "job_cancelled":
        await db.execute(
            "UPDATE worker_jobs SET status='CANCELLED' WHERE id=$1", job.id
        )
    elif mode == "trial_cancelled":
        await db.execute(
            "UPDATE trials SET status='FAILED', harbor_stage='cancelled' WHERE id='trial-1'"
        )
    elif mode == "exhausted":
        await db.execute("UPDATE worker_jobs SET max_attempts=1 WHERE id=$1", job.id)
    elif mode == "new_owner":
        await db.execute(
            "UPDATE worker_jobs SET current_worker_id='new', attempts=2 WHERE id=$1",
            job.id,
        )
    changed = await recover(
        job, container="ta-other" if mode == "wrong_container" else "ta-old"
    )
    assert changed == (mode in ("trial_cancelled", "exhausted"))
    status = await db.fetchval(
        "SELECT status::text FROM worker_jobs WHERE id=$1", job.id
    )
    assert (
        status
        == {
            "job_cancelled": "CANCELLED",
            "trial_cancelled": "CANCELLED",
            "exhausted": "FAILED",
            "wrong_container": "RUNNING",
            "new_owner": "RUNNING",
        }[mode]
    )
    if mode == "trial_cancelled":
        assert (
            await db.fetchval("SELECT harbor_stage FROM trials WHERE id='trial-1'")
            == "cancelled"
        )


@pytest.mark.asyncio
async def test_recovery_preserves_slot_reassigned_to_another_worker(claimed):
    db, job = claimed
    await db.execute("UPDATE queue_slots SET locked_by='another-worker'")
    assert await recover(job)
    assert await db.fetchval("SELECT locked_by FROM queue_slots") == "another-worker"


@pytest.mark.asyncio
async def test_locked_trial_defers_entire_recovery(claimed):
    db, job = claimed
    async with db.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT id FROM trials WHERE id='trial-1' FOR UPDATE"
            )
            assert not await recover(job)
    assert (
        await db.fetchval("SELECT status::text FROM worker_jobs WHERE id=$1", job.id)
        == "RUNNING"
    )
    assert await db.fetchval("SELECT outcome FROM worker_resource_attempts") is None
    assert await db.fetchval("SELECT locked_by FROM queue_slots") == "old"


@pytest.mark.asyncio
async def test_heartbeat_fallback_records_interruption_without_invented_stop_time(
    claimed,
):
    db, job = claimed
    await db.execute(
        "UPDATE worker_jobs SET heartbeat_at=NOW()-interval '20 minutes' WHERE id=$1",
        job.id,
    )
    async with cleanup.get_session() as session:
        retried, _, _, _ = await cleanup._reap_stale_worker_jobs(
            session, stale_after_minutes=15
        )
    assert retried == 1
    row = await db.fetchrow("SELECT outcome, finished_at FROM worker_resource_attempts")
    assert row["outcome"] == "INTERRUPTED"
    assert row["finished_at"] is None
