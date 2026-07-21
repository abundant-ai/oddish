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
from typing import Callable

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


_DEFAULT_TASK_TOML = """\
version = "1.0"

[metadata]
difficulty = "easy"
description = "a sample task"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
network_mode = "no-network"
"""


@pytest.fixture
def make_task(tmp_path: Path) -> Callable[..., Path]:
    """Build a minimal, valid Harbor task directory under tmp_path.

    Returns a factory so a single test can build several tasks. Every keyword
    overrides one file; ``None`` omits the file entirely.
    """

    def _make(
        name: str = "sample-task",
        *,
        task_toml: str | None = _DEFAULT_TASK_TOML,
        dockerfile: str | None = "FROM ubuntu:24.04\n",
        test_sh: str | None = "#!/bin/sh\nexit 0\n",
        solve_sh: str | None = None,
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        task_dir = tmp_path / name
        task_dir.mkdir(parents=True, exist_ok=True)

        if task_toml is not None:
            (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")
        (task_dir / "instruction.md").write_text("Solve the task.\n", encoding="utf-8")

        if dockerfile is not None:
            (task_dir / "environment").mkdir(exist_ok=True)
            (task_dir / "environment" / "Dockerfile").write_text(
                dockerfile, encoding="utf-8"
            )
        if test_sh is not None:
            (task_dir / "tests").mkdir(exist_ok=True)
            (task_dir / "tests" / "test.sh").write_text(test_sh, encoding="utf-8")
        if solve_sh is not None:
            (task_dir / "solution").mkdir(exist_ok=True)
            (task_dir / "solution" / "solve.sh").write_text(solve_sh, encoding="utf-8")

        for rel, content in (extra_files or {}).items():
            dest = task_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        return task_dir

    return _make
