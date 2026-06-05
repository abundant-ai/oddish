"""Regression test: cleanup finalizes tasks wedged in ANALYZING with no live trials.

The stage-advance passes in ``cleanup_orphaned_queue_state`` only feed
``maybe_start_*_stage`` trial ids drawn from live (non-superseded, non-deleted)
trials, so a task whose trials were all superseded/deleted can never leave
ANALYZING and keeps queued ANALYSIS jobs alive forever. The dedicated backstop
must finalize those tasks (FAILED) and cancel the dangling analysis jobs.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import cleanup  # noqa: E402


class _Result:
    def __init__(self, *, rows: list[Any] | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _RecordingSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement, params: dict[str, Any] | None = None):
        sql = str(statement)
        self.executed.append((sql, params or {}))

        if "SET    status = CASE" in sql:
            # Stale-heartbeat sweep -> nothing reaped.
            return _Result(rows=[])
        if ":stale_minutes" in sql:
            # The stuck-ANALYZING discovery SELECT.
            return _Result(rows=[("task-stuck-1",), ("task-stuck-2",)])
        if "UPDATE tasks" in sql and "status = 'FAILED'" in sql:
            return _Result(rowcount=2)
        if "w.kind::text = 'ANALYSIS'" in sql:
            return _Result(rowcount=5)
        if "UPDATE trials" in sql and "analysis_status = 'FAILED'" in sql:
            return _Result(rowcount=2)
        return _Result()

    async def get(self, model, object_id):  # noqa: ANN001
        return None

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_cleanup_finalizes_stuck_analyzing_tasks(monkeypatch):
    session = _RecordingSession()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_reap_zombies():
        return 0

    async def no_stage_transition(_session, _trial_id):
        return False

    monkeypatch.setattr(cleanup, "get_session", fake_get_session)
    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", fake_reap_zombies)
    monkeypatch.setattr("oddish.queue.maybe_start_analysis_stage", no_stage_transition)
    monkeypatch.setattr("oddish.queue.maybe_start_verdict_stage", no_stage_transition)

    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert result["stuck_analyzing_tasks_failed"] == 2
    assert result["stuck_analysis_jobs_cancelled"] == 5

    # The discovery SELECT ran with the configured staleness + batch bounds.
    discovery = next(
        (sql, params) for sql, params in session.executed if ":stale_minutes" in sql
    )
    assert discovery[1]["stale_minutes"] == cleanup.STUCK_ANALYZING_MINUTES
    assert discovery[1]["batch_limit"] == cleanup.STUCK_ANALYZING_BATCH_LIMIT

    # Both discovered task ids were finalized, and their analysis jobs cancelled.
    finalize = next(
        (sql, params)
        for sql, params in session.executed
        if "UPDATE tasks" in sql and "status = 'FAILED'" in sql
    )
    assert finalize[1]["task_ids"] == ["task-stuck-1", "task-stuck-2"]

    cancel = next(
        (sql, params)
        for sql, params in session.executed
        if "w.kind::text = 'ANALYSIS'" in sql
    )
    assert cancel[1]["task_ids"] == ["task-stuck-1", "task-stuck-2"]


@pytest.mark.asyncio
async def test_cleanup_skips_finalize_when_no_stuck_tasks(monkeypatch):
    session = _RecordingSession()
    # Override discovery to return no stuck tasks.
    original_execute = session.execute

    async def execute_no_stuck(statement, params=None):
        sql = str(statement)
        if ":stale_minutes" in sql:
            session.executed.append((sql, params or {}))
            return _Result(rows=[])
        return await original_execute(statement, params)

    session.execute = execute_no_stuck  # type: ignore[method-assign]

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_reap_zombies():
        return 0

    async def no_stage_transition(_session, _trial_id):
        return False

    monkeypatch.setattr(cleanup, "get_session", fake_get_session)
    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", fake_reap_zombies)
    monkeypatch.setattr("oddish.queue.maybe_start_analysis_stage", no_stage_transition)
    monkeypatch.setattr("oddish.queue.maybe_start_verdict_stage", no_stage_transition)

    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert result["stuck_analyzing_tasks_failed"] == 0
    assert result["stuck_analysis_jobs_cancelled"] == 0
    # No finalize UPDATE should have been issued.
    assert not any(
        "UPDATE tasks" in sql and "status = 'FAILED'" in sql
        for sql, _ in session.executed
    )
