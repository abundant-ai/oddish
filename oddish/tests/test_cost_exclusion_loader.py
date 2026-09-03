from __future__ import annotations

import pytest

from oddish.core.cost_exclusions import CostExclusions, load_cost_exclusions


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
