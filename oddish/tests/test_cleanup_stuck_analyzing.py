"""Regression test: cleanup unwedges ANALYZING tasks blocked by NULL-analysis trials.

A task with run_analysis stays in ANALYZING forever when a live trial's
``analysis_status`` is NULL (analysis was never enqueued for it), because
``maybe_start_verdict_stage`` reads NULL as "still pending". The backstop pass
must mark that lingering NULL analysis terminal and then advance the task to the
verdict stage (or finalize FAILED when no live trials remain).
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

    def scalar(self) -> Any:
        # The only ``.scalar()`` call in the sweep is the advisory-lock
        # acquire; returning truthy means "lock acquired -> run normally".
        return True


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
            # Discovery: one task with a live trial, one with none left.
            return _Result(rows=[("task-live", "trial-live"), ("task-empty", None)])
        if "UPDATE trials" in sql and "analysis_status = 'FAILED'" in sql:
            return _Result(rowcount=4)
        if "UPDATE tasks" in sql and "status = 'FAILED'" in sql:
            return _Result(rowcount=1)
        return _Result()

    async def get(self, model, object_id):  # noqa: ANN001
        return None

    async def flush(self) -> None:
        return None


def _patch_common(monkeypatch, session, verdict_fn):
    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_reap_zombies():
        return 0

    async def no_op(_session, _trial_id):
        return False

    monkeypatch.setattr(cleanup, "get_session", fake_get_session)
    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", fake_reap_zombies)
    monkeypatch.setattr("oddish.queue.maybe_start_analysis_stage", no_op)
    monkeypatch.setattr("oddish.queue.maybe_start_verdict_stage", verdict_fn)


@pytest.mark.asyncio
async def test_cleanup_unwedges_stuck_analyzing(monkeypatch):
    session = _RecordingSession()
    advanced_calls: list[str] = []

    async def fake_verdict(_session, trial_id):
        advanced_calls.append(trial_id)
        return True

    _patch_common(monkeypatch, session, fake_verdict)

    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert result["stuck_analysis_nulls_failed"] == 4
    assert result["stuck_analyzing_advanced"] == 1  # task-live -> verdict
    assert result["stuck_analyzing_finalized"] == 1  # task-empty -> FAILED
    # Only the task that still has a live trial is advanced via the verdict stage.
    assert advanced_calls == ["trial-live"]

    discovery = next(
        (sql, params) for sql, params in session.executed if ":stale_minutes" in sql
    )
    assert discovery[1]["stale_minutes"] == cleanup.STUCK_ANALYZING_MINUTES
    assert discovery[1]["batch_limit"] == cleanup.STUCK_ANALYZING_BATCH_LIMIT


@pytest.mark.asyncio
async def test_cleanup_noop_when_no_stuck_tasks(monkeypatch):
    session = _RecordingSession()

    async def execute_none(statement, params=None):
        sql = str(statement)
        session.executed.append((sql, params or {}))
        if "SET    status = CASE" in sql:
            return _Result(rows=[])
        if ":stale_minutes" in sql:
            return _Result(rows=[])
        return _Result()

    session.execute = execute_none  # type: ignore[method-assign]

    async def unexpected_verdict(_session, _trial_id):
        raise AssertionError("no task should be advanced when none are stuck")

    _patch_common(monkeypatch, session, unexpected_verdict)

    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert result["stuck_analyzing_advanced"] == 0
    assert result["stuck_analyzing_finalized"] == 0
    assert result["stuck_analysis_nulls_failed"] == 0
    assert not any(
        "UPDATE trials" in sql and "analysis_status = 'FAILED'" in sql
        for sql, _ in session.executed
    )
