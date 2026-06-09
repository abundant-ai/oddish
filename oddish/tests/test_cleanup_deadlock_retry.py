"""Regression tests: the cleanup sweep survives lock-race deadlocks.

The sweep is a single multi-table transaction and can lose a lock race
against a concurrent trial handler (Postgres ``deadlock detected``,
SQLSTATE 40P01). Two guards cover this:

* an advisory lock so two sweeps never run the bulk UPDATEs at once
  (the second sweep skips the tick), and
* a bounded deadlock-retry wrapper that re-runs the idempotent sweep
  from a fresh session.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import cleanup  # noqa: E402


class _FakeOrig(Exception):
    """Stand-in for the asyncpg error SQLAlchemy wraps in ``.orig``."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"db error sqlstate={sqlstate}")
        self.sqlstate = sqlstate


def _dbapi_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("UPDATE trials ...", {}, _FakeOrig(sqlstate))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Keep the retry backoff from actually sleeping during tests."""

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(cleanup.asyncio, "sleep", fake_sleep)


@pytest.mark.asyncio
async def test_deadlock_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sentinel = {"worker_jobs_retried": 7}

    async def flaky_once(*, stale_after_minutes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _dbapi_error("40P01")  # deadlock_detected
        return sentinel

    monkeypatch.setattr(cleanup, "_cleanup_orphaned_queue_state_once", flaky_once)

    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert result is sentinel
    assert calls["n"] == 2  # one deadlock, one success


@pytest.mark.asyncio
async def test_serialization_failure_is_retried(monkeypatch):
    calls = {"n": 0}

    async def flaky_once(*, stale_after_minutes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _dbapi_error("40001")  # serialization_failure
        return {"ok": 1}

    monkeypatch.setattr(cleanup, "_cleanup_orphaned_queue_state_once", flaky_once)

    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert result == {"ok": 1}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_non_retryable_error_propagates_immediately(monkeypatch):
    calls = {"n": 0}

    async def always_fails(*, stale_after_minutes):
        calls["n"] += 1
        raise _dbapi_error("23505")  # unique_violation -> not retryable

    monkeypatch.setattr(cleanup, "_cleanup_orphaned_queue_state_once", always_fails)

    with pytest.raises(DBAPIError):
        await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert calls["n"] == 1  # no retry on a real data error


@pytest.mark.asyncio
async def test_persistent_deadlock_gives_up_after_max_attempts(monkeypatch):
    calls = {"n": 0}

    async def always_deadlocks(*, stale_after_minutes):
        calls["n"] += 1
        raise _dbapi_error("40P01")

    monkeypatch.setattr(cleanup, "_cleanup_orphaned_queue_state_once", always_deadlocks)

    with pytest.raises(DBAPIError):
        await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert calls["n"] == cleanup.CLEANUP_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Advisory-lock skip path (exercises the real sweep body).
# ---------------------------------------------------------------------------


class _LockResult:
    def __init__(self, *, scalar_value: Any = None) -> None:
        self._scalar_value = scalar_value

    def scalar(self) -> Any:
        return self._scalar_value

    def mappings(self) -> "_LockResult":
        return self

    def all(self) -> list[Any]:
        return []

    rowcount = 0


class _LockHeldSession:
    """A session where the advisory-lock acquire returns False."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement, params: dict[str, Any] | None = None):
        sql = str(statement)
        self.statements.append(sql)
        if "pg_try_advisory_xact_lock" in sql:
            return _LockResult(scalar_value=False)
        return _LockResult()

    async def get(self, *_args, **_kwargs):
        return None

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_skips_tick_when_advisory_lock_held(monkeypatch):
    session = _LockHeldSession()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_reap_zombies():
        return 0

    monkeypatch.setattr(cleanup, "get_session", fake_get_session)
    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", fake_reap_zombies)

    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    # Returns a fully-zeroed result and never touches the bulk UPDATEs.
    assert result["terminal_trial_runtime_refs_cleared"] == 0
    assert result["experiments_last_activity_reconciled"] == 0
    assert all(value == 0 for value in result.values())
    assert any("pg_try_advisory_xact_lock" in sql for sql in session.statements)
    assert not any("UPDATE trials" in sql for sql in session.statements)
