"""``load_cost_exclusions``: cached per process, read through the
optional-table helper, and never persisting a "table missing" answer."""

from __future__ import annotations

import pytest

from oddish.config import settings
from oddish.core.cost_exclusions import (
    CostExclusions,
    invalidate_cost_exclusions,
    load_cost_exclusions,
)


class _NestedTransaction:
    def __init__(self):
        self.entered = False

    async def __aenter__(self):
        self.entered = True

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ExclusionSession:
    def __init__(self, *, is_read_autocommit: bool):
        self.info = {"oddish_read_autocommit": True} if is_read_autocommit else {}
        self.nested = _NestedTransaction()
        self.scalar_calls = 0

    def begin_nested(self):
        if self.info.get("oddish_read_autocommit") is True:
            raise AssertionError("autocommit reads must not create a savepoint")
        return self.nested

    async def scalars(self, _query):
        self.scalar_calls += 1
        return ()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(settings, "cost_exclusions_cache_seconds", 60.0)
    invalidate_cost_exclusions()
    yield
    invalidate_cost_exclusions()


@pytest.mark.asyncio
async def test_cost_exclusion_loader_skips_savepoint_in_autocommit():
    session = _ExclusionSession(is_read_autocommit=True)

    exclusions = await load_cost_exclusions(session)  # type: ignore[arg-type]

    assert exclusions == CostExclusions()
    assert session.scalar_calls == 3
    assert session.nested.entered is False


@pytest.mark.asyncio
async def test_cost_exclusion_loader_keeps_savepoint_in_transaction():
    session = _ExclusionSession(is_read_autocommit=False)

    exclusions = await load_cost_exclusions(session)  # type: ignore[arg-type]

    assert exclusions == CostExclusions()
    assert session.scalar_calls == 3
    assert session.nested.entered is True


@pytest.mark.asyncio
async def test_second_load_is_served_from_cache_without_statements():
    first = _ExclusionSession(is_read_autocommit=False)
    second = _ExclusionSession(is_read_autocommit=True)

    await load_cost_exclusions(first)  # type: ignore[arg-type]
    cached = await load_cost_exclusions(second)  # type: ignore[arg-type]

    assert cached == CostExclusions()
    assert second.scalar_calls == 0


@pytest.mark.asyncio
async def test_invalidate_forces_a_reload():
    first = _ExclusionSession(is_read_autocommit=False)
    second = _ExclusionSession(is_read_autocommit=False)

    await load_cost_exclusions(first)  # type: ignore[arg-type]
    invalidate_cost_exclusions()
    await load_cost_exclusions(second)  # type: ignore[arg-type]

    assert second.scalar_calls == 3


@pytest.mark.asyncio
async def test_zero_ttl_disables_the_cache(monkeypatch):
    monkeypatch.setattr(settings, "cost_exclusions_cache_seconds", 0.0)
    first = _ExclusionSession(is_read_autocommit=False)
    second = _ExclusionSession(is_read_autocommit=False)

    await load_cost_exclusions(first)  # type: ignore[arg-type]
    await load_cost_exclusions(second)  # type: ignore[arg-type]

    assert second.scalar_calls == 3


@pytest.mark.asyncio
async def test_missing_table_answer_is_not_cached(monkeypatch):
    """The empty fallback must not stick for a minute after the migration lands."""
    from sqlalchemy.exc import ProgrammingError

    class _MissingTable(Exception):
        sqlstate = "42P01"

    class _BrokenSession(_ExclusionSession):
        async def scalars(self, _query):
            self.scalar_calls += 1
            raise ProgrammingError("SELECT 1", {}, _MissingTable())

    broken = _BrokenSession(is_read_autocommit=True)
    healthy = _ExclusionSession(is_read_autocommit=True)

    assert await load_cost_exclusions(broken) == CostExclusions()  # type: ignore[arg-type]
    await load_cost_exclusions(healthy)  # type: ignore[arg-type]

    assert healthy.scalar_calls == 3
