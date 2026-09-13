"""``fetch_visible_worker_jobs``: one statement, same rows and ranking as the
two-query shape it replaces."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from oddish.core.helpers import fetch_visible_worker_jobs
from oddish.db.models import (
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    utcnow,
)
from oddish.schemas import VisibleWorkerJob


@contextmanager
def count_statements():
    import oddish.db.connection as conn

    seen: list[str] = []

    def _record(_conn, _cursor, statement, *_):
        seen.append(statement)

    event.listen(conn.engine.sync_engine, "after_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(conn.engine.sync_engine, "after_cursor_execute", _record)


def _job(subject_id, *, status, created_at, finished_at=None, subject_table="trials"):
    return WorkerJobModel(
        id=generate_id(),
        kind=WorkerJobKind.TRIAL,
        status=status,
        queue_key="anthropic/claude-opus-4-8",
        subject_table=subject_table,
        subject_id=subject_id,
        attempts=1,
        max_attempts=3,
        created_at=created_at,
        finished_at=finished_at,
    )


@pytest.mark.asyncio
async def test_one_statement_returns_active_then_recent_terminal(session):
    trial_id = f"wj-{uuid.uuid4().hex[:8]}"
    other_id = f"wj-other-{uuid.uuid4().hex[:8]}"
    now = utcnow()
    older_active = _job(
        trial_id, status=WorkerJobStatus.QUEUED, created_at=now - timedelta(minutes=5)
    )
    newer_active = _job(
        trial_id, status=WorkerJobStatus.RUNNING, created_at=now - timedelta(minutes=1)
    )
    recent_done = _job(
        trial_id,
        status=WorkerJobStatus.SUCCESS,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=1),
    )
    more_recent_done = _job(
        trial_id,
        status=WorkerJobStatus.FAILED,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=10),
    )
    stale_done = _job(
        trial_id,
        status=WorkerJobStatus.SUCCESS,
        created_at=now - timedelta(days=3),
        finished_at=now - timedelta(days=2),
    )
    someone_elses = _job(other_id, status=WorkerJobStatus.RUNNING, created_at=now)
    session.add_all(
        [
            older_active,
            newer_active,
            recent_done,
            more_recent_done,
            stale_done,
            someone_elses,
        ]
    )
    await session.flush()

    with count_statements() as statements:
        jobs = await fetch_visible_worker_jobs(session, trial_ids=[trial_id])

    assert len(statements) == 1
    assert "UNION ALL" in statements[0]
    visible = jobs[("trials", trial_id)]
    assert all(isinstance(job, VisibleWorkerJob) for job in visible)
    assert [job.id for job in visible] == [
        newer_active.id,
        older_active.id,
        more_recent_done.id,
        recent_done.id,
    ]
    assert visible[0].status == "running"
    assert visible[0].kind == "trial"
    assert visible[0].queue_key
    assert ("trials", other_id) not in jobs


@pytest.mark.asyncio
async def test_active_only_skips_the_terminal_branch(session):
    trial_id = f"wj-{uuid.uuid4().hex[:8]}"
    now = utcnow()
    session.add_all(
        [
            _job(trial_id, status=WorkerJobStatus.BLOCKED, created_at=now),
            _job(
                trial_id,
                status=WorkerJobStatus.SUCCESS,
                created_at=now - timedelta(hours=1),
                finished_at=now - timedelta(minutes=5),
            ),
        ]
    )
    await session.flush()

    with count_statements() as statements:
        jobs = await fetch_visible_worker_jobs(
            session, trial_ids=[trial_id], include_recent_terminal=False
        )

    assert len(statements) == 1
    assert "UNION ALL" not in statements[0]
    assert [job.status for job in jobs[("trials", trial_id)]] == ["blocked"]


@pytest.mark.asyncio
async def test_no_subjects_means_no_statement(session):
    with count_statements() as statements:
        assert await fetch_visible_worker_jobs(session) == {}
    assert statements == []
