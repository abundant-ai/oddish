from __future__ import annotations

import pytest

from oddish.core.cost_exclusions import CostExclusions, load_cost_exclusions


class _Connection:
    def __init__(self, isolation_level: str | None):
        self.isolation_level = isolation_level

    def get_execution_options(self):
        return {"isolation_level": self.isolation_level}


class _NestedTransaction:
    def __init__(self):
        self.entered = False

    async def __aenter__(self):
        self.entered = True

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ExclusionSession:
    def __init__(self, isolation_level: str | None):
        self.connection_value = _Connection(isolation_level)
        self.nested = _NestedTransaction()
        self.scalar_calls = 0

    async def connection(self):
        return self.connection_value

    def begin_nested(self):
        if self.connection_value.isolation_level == "AUTOCOMMIT":
            raise AssertionError("autocommit reads must not create a savepoint")
        return self.nested

    async def scalars(self, _query):
        self.scalar_calls += 1
        return ()


@pytest.mark.asyncio
async def test_cost_exclusion_loader_skips_savepoint_in_autocommit():
    session = _ExclusionSession("AUTOCOMMIT")

    exclusions = await load_cost_exclusions(session)  # type: ignore[arg-type]

    assert exclusions == CostExclusions()
    assert session.scalar_calls == 3
    assert session.nested.entered is False


@pytest.mark.asyncio
async def test_cost_exclusion_loader_keeps_savepoint_in_transaction():
    session = _ExclusionSession(None)

    exclusions = await load_cost_exclusions(session)  # type: ignore[arg-type]

    assert exclusions == CostExclusions()
    assert session.scalar_calls == 3
    assert session.nested.entered is True
