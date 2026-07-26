"""Regression tests for stale TAG_PROJECT worker reconciliation."""

from __future__ import annotations

import os
from typing import Any
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text

import oddish.db.connection as _conn
from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus
from oddish.workers.queue import cleanup

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


class _Result:
    def __init__(
        self,
        *,
        rows: list[Any] | None = None,
        mapping: dict[str, Any] | None = None,
    ) -> None:
        self._rows = rows or []
        self._mapping = mapping

    def all(self) -> list[Any]:
        return self._rows

    def mappings(self) -> "_Result":
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._mapping


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _tag_row(*, status: str, superseded: bool) -> dict[str, Any]:
    return {
        "id": "tag-stale",
        "kind": "TAG_PROJECT",
        "new_status": status,
        "subject_table": "tasks",
        "subject_id": "task-1",
        "attempts": 1,
        "max_attempts": 6,
        "error_message": cleanup._TAG_PROJECT_SUPERSEDED_REASON,
        "provider": None,
        "external_id": None,
        "tag_project_superseded": superseded,
    }


class _ExistingFollowupSession:
    def __init__(self) -> None:
        self.transition_sql = ""
        self.transition_params: list[dict[str, Any]] = []

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, statement, params=None):
        sql = str(statement)
        if sql.lstrip().startswith("SELECT id FROM worker_jobs"):
            return _Result(rows=[("tag-stale",)])
        if "WITH candidate AS" in sql:
            self.transition_sql = sql
            self.transition_params.append(params or {})
            return _Result(mapping=_tag_row(status="FAILED", superseded=True))
        return _Result()

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stale_tag_project_preserves_queued_followup():
    session = _ExistingFollowupSession()

    retried, failed, trial_ids, worker_targets = (
        await cleanup._reap_stale_worker_jobs(session, stale_after_minutes=15)
    )

    assert retried == 0
    assert failed == 1
    assert trial_ids == []
    assert worker_targets == set()
    assert "followup.status" in session.transition_sql
    assert "followup.status::text" not in session.transition_sql
    assert "IN ('QUEUED', 'RETRYING')" in session.transition_sql
    assert session.transition_params[0]["force_tag_project_terminal"] is False


class _ConstraintCollision(Exception):
    constraint_name = cleanup._TAG_PROJECT_ACTIVE_CONSTRAINT


class _ConcurrentFollowupSession(_ExistingFollowupSession):
    async def execute(self, statement, params=None):
        sql = str(statement)
        if sql.lstrip().startswith("SELECT id FROM worker_jobs"):
            return _Result(rows=[("tag-stale",)])
        if "WITH candidate AS" in sql:
            self.transition_params.append(params or {})
            if len(self.transition_params) == 1:
                raise IntegrityError(
                    "UPDATE worker_jobs",
                    params or {},
                    _ConstraintCollision("duplicate active TAG_PROJECT"),
                )
            return _Result(mapping=_tag_row(status="FAILED", superseded=True))
        return _Result()


@pytest.mark.asyncio
async def test_concurrent_tag_project_followup_terminalizes_stale_worker():
    session = _ConcurrentFollowupSession()

    retried, failed, trial_ids, worker_targets = (
        await cleanup._reap_stale_worker_jobs(session, stale_after_minutes=15)
    )

    assert retried == 0
    assert failed == 1
    assert trial_ids == []
    assert worker_targets == set()
    assert [
        params["force_tag_project_terminal"] for params in session.transition_params
    ] == [False, True]


@requires_db
@pytest.mark.asyncio
async def test_transition_sql_preserves_existing_tag_project_followup():
    suffix = uuid.uuid4().hex[:8]
    stale_id = f"tag-stale-{suffix}"
    followup_id = f"tag-followup-{suffix}"
    subject_id = f"task-{suffix}"

    async with _conn.async_session_maker() as session:
        session.add_all(
            [
                WorkerJobModel(
                    id=stale_id,
                    kind=WorkerJobKind.TAG_PROJECT,
                    status=WorkerJobStatus.RUNNING,
                    queue_key="tag-project",
                    subject_table="tasks",
                    subject_id=subject_id,
                    attempts=1,
                    max_attempts=6,
                ),
                WorkerJobModel(
                    id=followup_id,
                    kind=WorkerJobKind.TAG_PROJECT,
                    status=WorkerJobStatus.QUEUED,
                    queue_key="tag-project",
                    subject_table="tasks",
                    subject_id=subject_id,
                    attempts=0,
                    max_attempts=6,
                ),
            ]
        )
        await session.commit()

    try:
        async with _conn.async_session_maker() as session:
            row = await cleanup._transition_stale_worker_job(
                session,
                job_id=stale_id,
                stale_after_minutes=15,
            )
            await session.commit()

        assert row is not None
        assert row["new_status"] == "FAILED"
        assert row["tag_project_superseded"] is True
        assert row["error_message"] == cleanup._TAG_PROJECT_SUPERSEDED_REASON

        async with _conn.async_session_maker() as session:
            followup_status = (
                await session.execute(
                    text("SELECT status::text FROM worker_jobs WHERE id = :id"),
                    {"id": followup_id},
                )
            ).scalar_one()
        assert followup_status == "QUEUED"
    finally:
        async with _conn.async_session_maker() as session:
            await session.execute(
                text("DELETE FROM worker_jobs WHERE id = ANY(:ids)"),
                {"ids": [stale_id, followup_id]},
            )
            await session.commit()
