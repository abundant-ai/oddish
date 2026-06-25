"""Shared pytest fixtures and configuration.

pytest-asyncio gives each test function its own event loop. The SQLAlchemy
asyncpg engine is created at import time and is loop-bound once it opens a
connection, so a second async test that reuses the module-level engine fails
with "Event loop is closed". This autouse fixture recreates the engine before
every test so each test gets a fresh engine bound to its own loop.

The recreated engine uses NullPool (no pooled connections) so recreating it per
test leaks nothing -- connections are opened and closed per use.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest_asyncio.fixture
async def session():
    """Async DB session that rolls back after each test, leaving the DB clean."""
    import oddish.db.connection as _conn

    async with _conn.async_session_maker() as s:
        try:
            yield s
        finally:
            await s.rollback()


@pytest.fixture(autouse=True)
def _recycle_db_engine():
    """Recreate the shared async engine (NullPool) before each test.

    Runs for every test. For tests that never open a DB connection this is a
    cheap no-op object swap; for async DB tests it guarantees the engine is
    bound to the current test's event loop with no pooled connections to leak.
    """
    import oddish.db.connection as _conn
    from oddish.config import Settings

    Settings.db_use_null_pool = True
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)
    yield
