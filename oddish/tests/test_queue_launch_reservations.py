"""Real PostgreSQL races. ODDISH_TEST_DATABASE_URL must name a disposable DB."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from contextlib import asynccontextmanager
from functools import partial
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from oddish.db.models import Base
from oddish.dispatch.cycle import build_dispatch_plan
from oddish.workers.queue import slots, worker_job_dispatcher as dispatcher
from oddish.workers.queue import worker_job_single_job as runner


@pytest_asyncio.fixture
async def database(monkeypatch):
    url = os.environ.get("ODDISH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable ODDISH_TEST_DATABASE_URL")
    schema = "qa_dispatch_" + uuid4().hex
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=8, server_settings={"search_path": schema}
    )

    async def connect():
        return await asyncpg.connect(dsn, server_settings={"search_path": schema})

    @asynccontextmanager
    async def slot_connection():
        conn = await connect()
        try:
            yield conn
        finally:
            await conn.close()

    async def get_pool():
        return pool

    monkeypatch.setattr(slots, "_slot_connection", slot_connection)
    monkeypatch.setattr(dispatcher, "get_pool", get_pool)
    monkeypatch.setattr(runner, "_open_connection", connect)
    from oddish.workers.jobs import ensure_builtin_handlers_registered

    ensure_builtin_handlers_registered()
    try:
        yield pool
    finally:
        await pool.close()
        await engine.dispose()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def enqueue(
    pool, count=300, priority=1, key="sonnet", org="org-a", status="QUEUED", delay=0
):
    await pool.execute(
        """
        INSERT INTO worker_jobs (id, kind, status, queue_key, org_id, priority, available_after, created_at, updated_at)
        SELECT gen_random_uuid()::text, 'TRIAL', $1::worker_job_status, $2, $3, $4,
               NOW() + make_interval(secs => $5), NOW(), NOW()
        FROM generate_series(1, $6)
    """,
        status,
        key,
        org,
        priority,
        delay,
        count,
    )


def plan_factory(limit=600, max_workers=256):
    async def limits(keys):
        return dict.fromkeys(keys, limit)

    return partial(
        build_dispatch_plan, max_workers=max_workers, concurrency_limits_for=limits
    )


@pytest.mark.asyncio
async def test_overlapping_cycles_reserve_demand_once_and_share_model_limit(database):
    await enqueue(database)
    await enqueue(database, priority=0)
    batches = await asyncio.gather(
        *(slots.reserve_queue_launches(plan_factory(limit=256)) for _ in range(4))
    )
    launches = [r for _, batch in batches for r in batch]
    assert len(launches) == 256
    assert Counter(r.unit.priority_class for r in launches) == {True: 192, False: 64}
    assert await slots.count_held_queue_slots(["sonnet"]) == {"sonnet": 256}
    assert len({r.token for r in launches}) == 256


@pytest.mark.asyncio
async def test_pending_launches_do_not_duplicate_small_backlog(database):
    await enqueue(database, count=12)
    _, first = await slots.reserve_queue_launches(plan_factory())
    _, second = await slots.reserve_queue_launches(plan_factory())
    assert len(first) == 12
    assert second == []  # 588 free model slots, but all 12 jobs already have launches.
    r = first[0]
    slot = await slots.acquire_queue_slot(
        queue_key="sonnet",
        limit=600,
        worker_id="starting",
        lease_seconds=3600,
        reservation_token=r.token,
    )
    _, third = await slots.reserve_queue_launches(plan_factory())
    assert third == []  # Adoption has committed, but no job has been claimed yet.
    job = await runner.claim_single_worker_job(
        "sonnet",
        worker_id="starting",
        queue_slot=slot,
        priority_class=True,
        org_id="org-a",
    )
    assert job is not None
    assert (
        await database.fetchval(
            "SELECT launch_demand FROM queue_slots WHERE locked_by = 'starting'"
        )
        is None
    )
    _, fourth = await slots.reserve_queue_launches(plan_factory())
    assert fourth == []


@pytest.mark.asyncio
async def test_single_slot_polls_persist_priority_and_org_turns(database):
    for org in ("org-a", "org-b"):
        await enqueue(database, count=10, org=org)
        await enqueue(database, count=10, org=org, priority=0)
    classes = {"org-a": [], "org-b": []}
    for _ in range(8):
        _, [reservation] = await slots.reserve_queue_launches(
            plan_factory(limit=1, max_workers=1)
        )
        classes[reservation.unit.org_id].append(reservation.unit.priority_class)
        await slots.release_launch_reservations([reservation.token])
    assert classes == {
        "org-a": [True, True, True, False],
        "org-b": [True, True, True, False],
    }


@pytest.mark.asyncio
async def test_adoption_release_expiry_and_stale_token_fencing(database):
    await enqueue(database, count=3)
    _, reservations = await slots.reserve_queue_launches(plan_factory(limit=3))
    first, failed, abandoned = reservations
    adopted = await slots.acquire_queue_slot(
        queue_key="sonnet",
        limit=3,
        worker_id="worker",
        lease_seconds=3600,
        reservation_token=first.token,
    )
    assert adopted == first.slot
    assert await slots.count_held_queue_slots(["sonnet"]) == {"sonnet": 3}
    # Late error/duplicate call cannot revoke or readopt an active worker lease.
    await slots.release_launch_reservations([first.token, failed.token])
    assert (
        await slots.acquire_queue_slot(
            queue_key="sonnet",
            limit=3,
            worker_id="duplicate",
            lease_seconds=3600,
            reservation_token=first.token,
        )
        is None
    )
    await database.execute(
        "UPDATE queue_slots SET locked_until = NOW() - interval '1 second' WHERE locked_by = $1",
        abandoned.token,
    )
    _, replacement = await slots.reserve_queue_launches(plan_factory(limit=3))
    assert len(replacement) == 2
    assert (
        await slots.acquire_queue_slot(
            queue_key="sonnet",
            limit=3,
            worker_id="late",
            lease_seconds=3600,
            reservation_token=abandoned.token,
        )
        is None
    )
    assert await slots.count_held_queue_slots(["sonnet"]) == {"sonnet": 3}
    await slots.release_queue_slot(queue_key="sonnet", slot=adopted, worker_id="worker")
    assert await slots.count_held_queue_slots(["sonnet"]) == {"sonnet": 2}


@pytest.mark.asyncio
async def test_blocked_delayed_and_disabled_queues_do_not_launch(database):
    await enqueue(database, count=3, status="BLOCKED")
    await enqueue(database, count=3, status="RETRYING", delay=300)
    _, batch = await slots.reserve_queue_launches(plan_factory())
    assert batch == []
    await enqueue(database, count=3, status="RETRYING", delay=-1)
    _, batch = await slots.reserve_queue_launches(plan_factory(limit=0))
    assert batch == []
    _, batch = await slots.reserve_queue_launches(plan_factory(limit=2))
    assert len(batch) == 2


@pytest.mark.asyncio
async def test_legacy_acquisitions_race_launches_without_exceeding_limit(database):
    await enqueue(database, count=30)
    results = await asyncio.gather(
        slots.reserve_queue_launches(plan_factory(limit=10)),
        *(
            slots.acquire_queue_slot(
                queue_key="sonnet",
                limit=10,
                worker_id=f"legacy-{i}",
                lease_seconds=3600,
            )
            for i in range(12)
        ),
    )
    reserved = len(results[0][1])
    acquired = sum(slot is not None for slot in results[1:])
    assert reserved + acquired == 10
    assert await slots.count_held_queue_slots(["sonnet"]) == {"sonnet": 10}


@pytest.mark.asyncio
async def test_claim_honors_allocated_org_class_and_existing_priority_order(database):
    await enqueue(database, count=1, priority=1)
    await enqueue(database, count=1, priority=2)
    await enqueue(database, count=1, priority=0)
    await enqueue(database, count=1, priority=3, org="org-b")
    await enqueue(database, count=1, priority=4, status="BLOCKED")
    await enqueue(database, count=1, priority=5, status="RETRYING", delay=300)
    kwargs = dict(worker_id="claim-worker", queue_slot=0, org_id="org-a")
    ordinary = await runner.claim_single_worker_job(
        "sonnet", priority_class=False, **kwargs
    )
    analysis = await runner.claim_single_worker_job(
        "sonnet", priority_class=True, **kwargs
    )
    assert ordinary and analysis
    rows = await database.fetch(
        "SELECT id, priority FROM worker_jobs WHERE status = 'RUNNING'"
    )
    assert {row["id"]: row["priority"] for row in rows} == {
        ordinary.id: 0,
        analysis.id: 2,
    }
    assert ordinary.org_id == analysis.org_id == "org-a"


@pytest.mark.asyncio
async def test_planning_failure_rolls_back_state_and_reservations(database):
    async def fail(**kwargs):
        kwargs["fairness_cursors"]["org-a"] = 99
        raise RuntimeError("planning failed")

    with pytest.raises(RuntimeError, match="planning failed"):
        await slots.reserve_queue_launches(fail)
    assert await database.fetchval("SELECT COUNT(*) FROM queue_dispatch_state") == 0
    assert await database.fetchval("SELECT COUNT(*) FROM queue_slots") == 0


@pytest_asyncio.fixture
async def schema_engine(database):
    schema = await database.fetchval("SELECT current_schema()")
    engine = create_async_engine(
        os.environ["ODDISH_TEST_DATABASE_URL"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_orphan_cleanup_preserves_pending_launch_until_expiry(
    database, schema_engine
):
    from sqlalchemy.ext.asyncio import AsyncSession
    from oddish.workers.queue.cleanup import _release_orphaned_slots

    await enqueue(database, count=2)
    _, [reservation, starting] = await slots.reserve_queue_launches(
        plan_factory(limit=2)
    )
    orphan = await slots.acquire_queue_slot(
        queue_key="sonnet",
        limit=2,
        worker_id="orphan",
        lease_seconds=3600,
        reservation_token=starting.token,
    )
    await database.execute(
        "UPDATE queue_slots SET locked_at = NOW() - interval '3 minutes'"
    )
    async with AsyncSession(schema_engine) as session:
        assert await _release_orphaned_slots(session) == 1
        await session.commit()
    assert (
        await database.fetchval(
            "SELECT locked_by FROM queue_slots WHERE slot = $1", orphan
        )
        is None
    )
    assert (
        await database.fetchval(
            "SELECT locked_by FROM queue_slots WHERE slot = $1", reservation.slot
        )
        == reservation.token
    )
    await database.execute(
        "UPDATE queue_slots SET locked_until = NOW() - interval '1 second'"
    )
    assert await slots.cleanup_stale_queue_slots() == 1
    assert await slots.count_held_queue_slots(["sonnet"]) == {}


@pytest.mark.asyncio
async def test_migration_upgrades_old_schema_and_is_create_all_safe(
    database, schema_engine
):
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from pathlib import Path

    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    migration = (
        ScriptDirectory.from_config(config).get_revision("qa_dispatch_001").module
    )
    async with schema_engine.begin() as conn:

        def check(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                migration.upgrade()  # create_all already has the columns/table.
                migration.downgrade()
                migration.upgrade()  # existing deployment starts without them.

        await conn.run_sync(check)
    await enqueue(database, count=1)
    _, batch = await slots.reserve_queue_launches(plan_factory())
    assert len(batch) == 1


CANDIDATE_CONFIG = "candidate-cpu0.6-mem3072"


async def seed_resource_trials(pool, count=20):
    # Copy a normal task shape into this test's isolated PostgreSQL schema.
    await pool.execute(
        "INSERT INTO worker_resource_rollout (id, fraction) VALUES (1, 1)"
    )
    await pool.execute(
        """
        INSERT INTO tasks (id, name, "user", task_path, status, priority, tags, run_analysis, run_probe, created_at, updated_at)
        VALUES ('task', 'copied task', 'tester', '/tmp/copied-task', 'PENDING', 'LOW', '{}'::jsonb, false, false, NOW(), NOW())
    """
    )
    await pool.execute(
        "INSERT INTO experiments (id, name, is_public, created_at, updated_at) VALUES ('experiment', 'copied test', false, NOW(), NOW())"
    )
    await pool.execute(
        """
        INSERT INTO trials (id, name, task_id, experiment_id, agent, model, provider, queue_key, status,
                            attempts, max_attempts, created_at, updated_at)
        SELECT 'trial-' || n, 'trial-' || n, 'task', 'experiment', 'codex', 'm', 'openai', 'm', 'PENDING',
               0, 3, NOW(), NOW() FROM generate_series(1, $1) n
    """,
        count,
    )
    await pool.execute(
        """
        INSERT INTO worker_jobs (id, kind, status, queue_key, org_id, priority,
            subject_table, subject_id, available_after, created_at, updated_at)
        SELECT 'job-' || n, 'TRIAL', 'QUEUED', 'm', 'org-a', 0,
               'trials', 'trial-' || n, NOW(), NOW(), NOW() FROM generate_series(1, $1) n
    """,
        count,
    )


async def reserve_candidates(limit=100):
    return await slots.reserve_queue_launches(
        plan_factory(limit=limit),
        candidate_configuration=CANDIDATE_CONFIG,
        candidate_max_workers=2,
    )


async def adopt_candidate(reservation, worker="candidate"):
    slot = await slots.acquire_queue_slot(
        queue_key="m",
        limit=100,
        worker_id=worker,
        lease_seconds=3600,
        reservation_token=reservation.token,
    )
    assert slot is not None
    return slot


@pytest.mark.asyncio
async def test_candidate_reservations_cap_pending_and_adopted_workers(database):
    await seed_resource_trials(database)
    batches = await asyncio.gather(*(reserve_candidates() for _ in range(3)))
    reservations = [r for _, batch in batches for r in batch]
    assert len(reservations) == 2
    assert all(r.unit.resource_candidate for r in reservations)
    slot = await adopt_candidate(reservations[0])
    job = await runner.claim_single_worker_job(
        "m",
        worker_id="candidate",
        queue_slot=slot,
        priority_class=False,
        org_id="org-a",
        resource_candidate=True,
        candidate_configuration=CANDIDATE_CONFIG,
    )
    assert job is not None
    assert (await reserve_candidates())[
        1
    ] == []  # first claim cleared launch_demand, not the worker cap
    await slots.release_launch_reservations([reservations[1].token])
    assert len((await reserve_candidates())[1]) == 1


@pytest.mark.asyncio
async def test_candidate_claims_exclude_other_kinds_images_orgs_and_priority(database):
    await seed_resource_trials(database, count=10)
    await database.execute("UPDATE trials SET kind='qa' WHERE id='trial-1'")
    await database.execute("UPDATE trials SET is_probe=true WHERE id='trial-2'")
    await database.execute("UPDATE worker_jobs SET priority=1 WHERE id='job-3'")
    await database.execute(
        "UPDATE worker_jobs SET harbor_variant_id='ephemeral' WHERE id='job-4'"
    )
    await database.execute(
        "UPDATE worker_jobs SET execution_lane='ec2_trial' WHERE id='job-5'"
    )
    await database.execute("UPDATE worker_jobs SET org_id='org-b' WHERE id='job-6'")
    await database.execute("UPDATE worker_jobs SET kind='TASK_EXPAND' WHERE id='job-7'")
    await database.execute("UPDATE trials SET deleted_at=NOW() WHERE id='trial-8'")
    await database.execute(
        "UPDATE worker_jobs SET available_after=NOW()+interval '1 day' WHERE id='job-9'"
    )
    _, reservations = await reserve_candidates()
    reservation = next(
        r
        for r in reservations
        if r.unit.resource_candidate and r.unit.org_id == "org-a"
    )
    slot = await adopt_candidate(reservation)
    kwargs = dict(
        worker_id="candidate",
        queue_slot=slot,
        priority_class=False,
        org_id="org-a",
        resource_candidate=True,
        candidate_configuration=CANDIDATE_CONFIG,
    )
    job = await runner.claim_single_worker_job("m", **kwargs)
    assert job.id == "job-10"
    assert await runner.claim_single_worker_job("m", **kwargs) is None


@pytest.mark.asyncio
async def test_disable_finishes_active_job_and_stops_next_batch_claim(
    database, monkeypatch
):
    from oddish.costs.recorder import WorkerBillingSpec
    from oddish.workers.jobs.registry import JobOutcome
    from oddish.db import WorkerJobKind
    from types import SimpleNamespace

    await seed_resource_trials(database, count=3)
    _, reservations = await reserve_candidates()
    slot = await adopt_candidate(reservations[0])
    executed = []

    async def run(job):
        executed.append(job.id)
        await database.execute(
            "UPDATE worker_resource_rollout SET fraction=0 WHERE id=1"
        )
        assert (
            await database.fetchval(
                "SELECT status::text FROM worker_jobs WHERE id=$1", job.id
            )
            == "RUNNING"
        )
        return JobOutcome.ok()

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setitem(runner.HANDLERS, WorkerJobKind.TRIAL, SimpleNamespace(run=run))
    monkeypatch.setattr(runner, "_ensure_handlers_registered", lambda: None)
    monkeypatch.setattr(runner, "open_worker_span", noop)
    monkeypatch.setattr(runner, "close_worker_span", noop)
    spec = WorkerBillingSpec(
        0.6, 3072, True, cpu_limit=17, configuration=CANDIDATE_CONFIG
    )
    processed = await runner.drain_worker_jobs(
        "m",
        worker_id="candidate",
        queue_slot=slot,
        budget_seconds=300,
        priority_class=False,
        org_id="org-a",
        resource_candidate=True,
        candidate_configuration=CANDIDATE_CONFIG,
        worker_billing_spec=spec,
        modal_function_call_id="fc-copy-test",
    )
    assert processed == 1
    assert (
        await database.fetchval(
            "SELECT status::text FROM worker_jobs WHERE id=$1", executed[0]
        )
        == "SUCCESS"
    )
    attempt = await database.fetchrow("SELECT * FROM worker_resource_attempts")
    assert (
        attempt["configuration"],
        attempt["cpu_request"],
        attempt["cpu_limit"],
        attempt["memory_mb"],
        attempt["modal_function_call_id"],
    ) == (CANDIDATE_CONFIG, 0.6, 17, 3072, "fc-copy-test")
    assert (
        await runner.claim_single_worker_job(
            "m",
            worker_id="base",
            queue_slot=99,
            resource_candidate=False,
            candidate_configuration=CANDIDATE_CONFIG,
            priority_class=False,
            org_id="org-a",
        )
        is not None
    )  # Stopped candidate jobs immediately return to base eligibility.


@pytest.mark.asyncio
async def test_one_percent_is_selected_by_job_id_and_retries_keep_bucket(database):
    from oddish.workers.queue.resource_rollout import candidate_queue_counts
    from oddish.costs.recorder import WorkerBillingSpec

    await seed_resource_trials(database, count=10000)
    await database.execute("UPDATE worker_resource_rollout SET fraction=.01 WHERE id=1")
    counts = await candidate_queue_counts(database, 0.01)
    sampled = sum(counts.values())
    assert 70 <= sampled <= 130
    _, reservations = await reserve_candidates()
    candidate = next(r for r in reservations if r.unit.resource_candidate)
    slot = await adopt_candidate(candidate)
    spec = WorkerBillingSpec(
        0.6, 3072, True, cpu_limit=17, configuration=CANDIDATE_CONFIG
    )
    kwargs = dict(
        worker_id="candidate",
        queue_slot=slot,
        resource_candidate=True,
        candidate_configuration=CANDIDATE_CONFIG,
        worker_billing_spec=spec,
        priority_class=False,
        org_id="org-a",
    )
    first = await runner.claim_single_worker_job(
        "m", modal_function_call_id="fc-first", **kwargs
    )
    await database.execute(
        "UPDATE worker_jobs SET status='RETRYING', created_at=NOW()-interval '1 day' WHERE id=$1",
        first.id,
    )
    second = await runner.claim_single_worker_job(
        "m", modal_function_call_id="fc-retry", **kwargs
    )
    assert second.id == first.id and second.attempts == 2
    records = await database.fetch(
        "SELECT attempt, modal_function_call_id FROM worker_resource_attempts WHERE worker_job_id=$1 ORDER BY attempt",
        first.id,
    )
    assert [tuple(r) for r in records] == [(1, "fc-first"), (2, "fc-retry")]
    # Base claims exclude the sampled IDs, even when draining many short jobs.
    base = await runner.claim_single_worker_job(
        "m",
        worker_id="base",
        queue_slot=99,
        resource_candidate=False,
        candidate_configuration=CANDIDATE_CONFIG,
        priority_class=False,
        org_id="org-a",
    )
    import hashlib

    assert int(hashlib.md5(base.id.encode()).hexdigest()[:8], 16) / 2**32 >= 0.01


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_sql",
    [
        "UPDATE worker_resource_rollout SET fraction=0",
        "UPDATE worker_resource_rollout SET max_workers=0",
        "UPDATE worker_resource_rollout SET configuration='candidate-cpu0.6-mem1536'",
        "DELETE FROM worker_resource_rollout",
    ],
)
async def test_pending_candidate_cannot_claim_after_stop_or_configuration_change(
    database, stop_sql
):
    await seed_resource_trials(database, count=2)
    _, reservations = await reserve_candidates()
    slot = await adopt_candidate(reservations[0])
    await database.execute(stop_sql)
    assert (
        await runner.claim_single_worker_job(
            "m",
            worker_id="candidate",
            queue_slot=slot,
            resource_candidate=True,
            candidate_configuration=CANDIDATE_CONFIG,
            priority_class=False,
            org_id="org-a",
        )
        is None
    )
    assert (
        await database.fetchval(
            "SELECT COUNT(*) FROM worker_jobs WHERE status='QUEUED'"
        )
        == 2
    )
    assert all(not r.unit.resource_candidate for r in (await reserve_candidates())[1])


@pytest.mark.asyncio
async def test_stop_commits_after_inflight_claim_policy_read(database):
    from oddish.workers.queue.resource_rollout import load_rollout

    await seed_resource_trials(database, count=1)
    async with database.acquire() as conn:
        async with conn.transaction():
            assert await load_rollout(
                conn, configuration=CANDIDATE_CONFIG, lock=True
            ) == (1, 2)
            stopping = asyncio.create_task(
                database.execute("UPDATE worker_resource_rollout SET fraction=0")
            )
            await asyncio.sleep(0.05)
            assert (
                not stopping.done()
            )  # Stop cannot acknowledge while an old-policy claim is in flight.
        await asyncio.wait_for(stopping, timeout=2)
    assert await load_rollout(database, configuration=CANDIDATE_CONFIG) == (0, 2)
