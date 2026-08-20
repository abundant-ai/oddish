"""Unit tests for ``oddish.db.deadlock`` (no database required)."""

from __future__ import annotations

import types

import pytest
from sqlalchemy.exc import DBAPIError

import oddish.db.deadlock as deadlock_mod
from oddish.db.deadlock import (
    is_transient_serialization_error,
    retry_deadlocks,
    run_with_deadlock_retry,
)


class FakeDeadlock(Exception):
    """Mimics asyncpg's DeadlockDetectedError (sqlstate 40P01)."""

    sqlstate = "40P01"


class FakeUniqueViolation(Exception):
    sqlstate = "23505"


def _dbapi(orig: BaseException) -> DBAPIError:
    return DBAPIError("UPDATE tasks SET ...", {}, orig)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_delay):
        return None

    monkeypatch.setattr(
        deadlock_mod, "asyncio", types.SimpleNamespace(sleep=_instant)
    )


def test_detects_wrapped_deadlock():
    assert is_transient_serialization_error(_dbapi(FakeDeadlock()))


def test_detects_deadlock_behind_cause_chain():
    # SQLAlchemy's asyncpg adapter often exposes the driver error via
    # __cause__ on an intermediate wrapper rather than a direct attribute.
    adapter = RuntimeError("adapted dbapi error")
    adapter.__cause__ = FakeDeadlock()
    assert is_transient_serialization_error(_dbapi(adapter))


def test_ignores_other_sqlstates():
    assert not is_transient_serialization_error(_dbapi(FakeUniqueViolation()))
    assert not is_transient_serialization_error(ValueError("boom"))


@pytest.mark.asyncio
async def test_retries_deadlock_then_succeeds():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _dbapi(FakeDeadlock())
        return "ok"

    assert await run_with_deadlock_retry(fn, what="test") == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_final_attempt_reraises():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise _dbapi(FakeDeadlock())

    with pytest.raises(DBAPIError):
        await run_with_deadlock_retry(fn, what="test", attempts=3)
    assert calls == 3


@pytest.mark.asyncio
async def test_non_deadlock_db_error_raises_immediately():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise _dbapi(FakeUniqueViolation())

    with pytest.raises(DBAPIError):
        await run_with_deadlock_retry(fn, what="test")
    assert calls == 1


@pytest.mark.asyncio
async def test_non_sqlalchemy_error_propagates_untouched():
    async def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await run_with_deadlock_retry(fn, what="test")


@pytest.mark.asyncio
async def test_decorator_preserves_call_shape_and_retries():
    calls = 0

    @retry_deadlocks(what="test")
    async def handler(*, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _dbapi(FakeDeadlock())
        return {"payload": payload}

    assert await handler(payload={"x": 1}) == {"payload": {"x": 1}}
    assert calls == 2
