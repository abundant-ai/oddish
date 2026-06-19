"""Server-side request idempotency for ``POST /tasks/sweep``.

Exercises the backend ``submission_idempotency`` migration and the dedup /
replay / TTL / lock policy in ``create_task_sweep_core``. Runs against the
dedicated Postgres in ``ODDISH_DATABASE_URL`` (see the worktree setup); skipped
when that is unset.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models  # noqa: F401  registers SubmissionIdempotency on the shared Base
from models import SubmissionIdempotency
from idempotency_store import SubmissionIdempotencyStore
from oddish.core.endpoints import create_task_sweep_core
from oddish.core.idempotency import (
    SWEEP_ROUTE,
    IdempotencyReplay,
    compute_request_hash,
    hash_idempotency_key,
)
from oddish.db.models import Base, TaskModel, TrialModel, utcnow
from oddish.schemas import AgentModelPair, TaskSweepSubmission

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set")

BACKEND_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


async def _reset_and_create_all() -> None:
    """Rebuild the full current schema from the ORM models.

    The backend migration chain's first step requires the OSS oddish ``tasks``
    table to exist, and the from-scratch oddish chain carries unrelated seeding
    drift, so we materialize the present-day schema directly and exercise just
    this migration's up/down SQL against it.
    """
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _relation_exists(name: str) -> bool:
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}
            )
            return result.scalar() is not None
    finally:
        await engine.dispose()


def test_migration_applies_and_downgrades_cleanly() -> None:
    """``downgrade -1`` drops the table + unique index, and re-applying recreates
    them cleanly (the migration's real up/down SQL run via Alembic)."""
    cfg = _alembic_config()
    asyncio.run(_reset_and_create_all())
    command.stamp(cfg, "head")

    # downgrade -1 runs this migration's downgrade().
    command.downgrade(cfg, "-1")
    assert not asyncio.run(_relation_exists("submission_idempotency"))
    assert not asyncio.run(_relation_exists("uq_submission_idempotency_org_route_key"))
    # Reversing one step leaves the preceding schema intact.
    assert asyncio.run(_relation_exists("organizations"))

    # upgrade head runs this migration's upgrade() -> table + unique index back.
    command.upgrade(cfg, "head")
    assert asyncio.run(_relation_exists("submission_idempotency"))
    assert asyncio.run(_relation_exists("uq_submission_idempotency_org_route_key"))


# ---------------------------------------------------------------------------
# Dedup / replay / TTL / lock behaviour in create_task_sweep_core
# ---------------------------------------------------------------------------

ORG = "org1"
TASK = "task1"
KEY = "client-key-abc"


@pytest_asyncio.fixture
async def maker(tmp_path):
    """Fresh schema + a pre-existing task, on the dedicated Postgres.

    The task already exists, so a submission with ``append_to_task=False``
    auto-flips to append mode in the core -- the exact path a retried "create"
    takes and the duplication the idempotency layer must prevent.
    """
    engine = create_async_engine(URL)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
        # ``create_all`` does not reproduce migration-only partial indexes; the
        # append path enqueues a tag-projection worker job via INSERT ... ON
        # CONFLICT that needs this one (see oddish migration aa00ta01core).
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_tag_project_active "
                "ON worker_jobs (kind, subject_table, subject_id) "
                "WHERE kind = 'TAG_PROJECT' "
                "AND status IN ('QUEUED', 'RETRYING') "
                "AND subject_id IS NOT NULL"
            )
        )

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("solve")
    (task_dir / "task.toml").write_text('version = "1.0"\n')
    async with session_maker() as session:
        session.add(
            TaskModel(id=TASK, name="t", user="u", org_id=ORG, task_path=str(task_dir))
        )
        await session.commit()
    try:
        yield session_maker
    finally:
        await engine.dispose()


def _submission(**overrides) -> TaskSweepSubmission:
    data: dict = dict(
        task_id=TASK,
        configs=[
            AgentModelPair(
                agent="claude-code", model="anthropic/claude-sonnet-4-6", n_trials=1
            )
        ],
        user="alice",
    )
    data.update(overrides)
    return TaskSweepSubmission(**data)


async def _submit(session_maker, submission, key: str | None = KEY):
    """Run one sweep submission in its own committed transaction (a request).

    Returns the core's result tuple on success; propagates ``IdempotencyReplay``
    or ``HTTPException`` so tests can assert on them. Commit is skipped when the
    core raises, mirroring the rollback a failed request gets.
    """
    async with session_maker() as session:
        result = await create_task_sweep_core(
            session,
            submission=submission,
            org_id=ORG,
            idempotency_key=key,
            idempotency_store=SubmissionIdempotencyStore(session),
        )
        await session.commit()
        return result


async def _trial_count(session_maker) -> int:
    async with session_maker() as session:
        result = await session.execute(
            select(func.count())
            .select_from(TrialModel)
            .where(TrialModel.task_id == TASK)
        )
        return result.scalar_one()


async def _only_record(session_maker) -> SubmissionIdempotency:
    async with session_maker() as session:
        result = await session.execute(select(SubmissionIdempotency))
        return result.scalars().one()


@pytest.mark.asyncio
async def test_first_submit_creates_trials_and_stores_response(maker) -> None:
    _, new_trials, _, _ = await _submit(maker, _submission())

    assert len(new_trials) == 1
    assert await _trial_count(maker) == 1

    record = await _only_record(maker)
    assert record.status == "completed"
    assert record.org_id == ORG
    assert record.route == SWEEP_ROUTE
    assert record.key_hash == hash_idempotency_key(KEY)
    assert record.response_json["trials_count"] == 1
    assert record.response_json["new_trial_ids"] == [t.id for t in new_trials]


@pytest.mark.asyncio
async def test_retry_replays_without_duplicate_trials(maker) -> None:
    _, new_trials, _, _ = await _submit(maker, _submission())
    original_ids = [t.id for t in new_trials]
    assert await _trial_count(maker) == 1

    # Identical retry: the stored response is replayed and -- crucially -- the
    # auto-append flip never runs, so no second set of trials is created.
    with pytest.raises(IdempotencyReplay) as excinfo:
        await _submit(maker, _submission())

    replayed = excinfo.value.response_json
    assert replayed["new_trial_ids"] == original_ids
    assert replayed["trials_count"] == 1
    assert await _trial_count(maker) == 1


@pytest.mark.asyncio
async def test_same_key_different_request_conflicts(maker) -> None:
    await _submit(maker, _submission())
    assert await _trial_count(maker) == 1

    # Same key, different spec (2 trials) -> 409, and no trials created.
    different = _submission(
        configs=[
            AgentModelPair(
                agent="claude-code", model="anthropic/claude-sonnet-4-6", n_trials=2
            )
        ]
    )
    with pytest.raises(HTTPException) as excinfo:
        await _submit(maker, different)
    assert excinfo.value.status_code == 409
    assert await _trial_count(maker) == 1


@pytest.mark.asyncio
async def test_expires_at_is_created_plus_24h(maker) -> None:
    await _submit(maker, _submission())
    record = await _only_record(maker)
    assert record.expires_at == record.created_at + timedelta(hours=24)


@pytest.mark.asyncio
async def test_expired_key_is_pruned_and_rerun(maker) -> None:
    await _submit(maker, _submission())
    assert await _trial_count(maker) == 1

    # Force the recorded key to be expired.
    async with maker() as session:
        await session.execute(
            update(SubmissionIdempotency).values(
                expires_at=utcnow() - timedelta(seconds=1)
            )
        )
        await session.commit()

    # The same key now re-runs (stale record pruned), creating a fresh trial set
    # rather than replaying the old response.
    _, new_trials, _, _ = await _submit(maker, _submission())
    assert len(new_trials) == 1
    assert await _trial_count(maker) == 2

    record = await _only_record(maker)
    assert record.status == "completed"
    assert record.expires_at > utcnow()


@pytest.mark.asyncio
async def test_in_progress_key_conflicts(maker) -> None:
    submission = _submission()

    # A concurrent request has reserved the key but not finished (in_progress).
    async with maker() as session:
        session.add(
            SubmissionIdempotency(
                org_id=ORG,
                route=SWEEP_ROUTE,
                key_hash=hash_idempotency_key(KEY),
                request_hash=compute_request_hash(submission),
                status="in_progress",
                expires_at=utcnow() + timedelta(hours=24),
            )
        )
        await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await _submit(maker, submission)
    assert excinfo.value.status_code == 409
    # The duplicate did not create a second set of trials.
    assert await _trial_count(maker) == 0
