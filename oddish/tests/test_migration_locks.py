"""Retry semantics of oddish.db.migration_locks.run_with_lock_retry."""

from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from oddish.db import migration_locks


def _lock_timeout_error() -> sa.exc.DBAPIError:
    return sa.exc.DBAPIError(
        "ALTER TABLE trials ADD COLUMN kind",
        None,
        Exception("canceling statement due to lock timeout"),
    )


class _FakeContext:
    @contextmanager
    def autocommit_block(self):
        yield


class _FakeOp:
    """Stands in for alembic.op: records executes, no real connection."""

    def __init__(self):
        self.executed = []

    def get_context(self):
        return _FakeContext()

    def execute(self, sql):
        self.executed.append(sql)


@pytest.fixture
def fake_op(monkeypatch):
    op = _FakeOp()
    monkeypatch.setattr(migration_locks, "op", op)
    # Lock-holder logging needs a real bind; stub it out.
    monkeypatch.setattr(migration_locks, "log_lock_holders", lambda table: None)
    return op


def test_succeeds_after_lock_timeouts(fake_op, monkeypatch):
    monkeypatch.setattr(migration_locks.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def step():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _lock_timeout_error()

    migration_locks.run_with_lock_retry(step, table_name="trials", attempts=5)
    assert calls["n"] == 3
    # Every attempt re-arms the short lock_timeout in its own transaction.
    assert all("lock_timeout" in sql for sql in fake_op.executed)
    assert len(fake_op.executed) == 3


def test_raises_after_exhausting_attempts(fake_op, monkeypatch):
    monkeypatch.setattr(migration_locks.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def step():
        calls["n"] += 1
        raise _lock_timeout_error()

    with pytest.raises(sa.exc.DBAPIError):
        migration_locks.run_with_lock_retry(step, table_name="trials", attempts=4)
    assert calls["n"] == 4


def test_non_lock_error_propagates_immediately(fake_op):
    calls = {"n": 0}

    def step():
        calls["n"] += 1
        raise sa.exc.DBAPIError("stmt", None, Exception("relation does not exist"))

    with pytest.raises(sa.exc.DBAPIError):
        migration_locks.run_with_lock_retry(step, table_name="trials", attempts=5)
    assert calls["n"] == 1
