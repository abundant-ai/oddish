"""Tests for the server-side zombie-transaction prevention.

These cover two independent defences added after the 2026-04-17 incident,
where a SIGKILL-during-cancel burst left 26 orphaned 'idle in transaction'
backends holding AccessShareLocks on `trials` for 1h43m -- blocking
heartbeats and DDL migrations:

- The `server_settings` shipped on every asyncpg connection so Postgres
  itself bounces zombies via `idle_in_transaction_session_timeout`.
- The `reap_idle_in_transaction_zombies` cleanup step as a last-resort
  safety net if the server-side GUC isn't honored (older pooler versions).
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.workers.queue import cleanup as cleanup_module  # noqa: E402


def test_asyncpg_server_settings_includes_idle_txn_timeout():
    """Every connection must carry the Postgres-side safety net."""
    server_settings = settings.asyncpg_server_settings()
    assert "idle_in_transaction_session_timeout" in server_settings
    # Stored as milliseconds-string because that's how Postgres expects it
    # when set via a SET statement / startup parameter.
    value = server_settings["idle_in_transaction_session_timeout"]
    assert value.isdigit(), f"expected a ms integer string, got {value!r}"
    assert int(value) >= 60_000, "timeout should be at least 1 minute"


def test_asyncpg_server_settings_includes_application_name():
    """The reaper filters by application_name; it must always be set."""
    server_settings = settings.asyncpg_server_settings()
    assert server_settings.get("application_name")


def test_asyncpg_server_settings_matches_settings_object():
    """Changing the settings field propagates to what asyncpg receives."""
    settings.db_application_name = "oddish-test-scope"
    settings.idle_in_transaction_session_timeout_ms = 123_456
    try:
        ss = settings.asyncpg_server_settings()
        assert ss["application_name"] == "oddish-test-scope"
        assert ss["idle_in_transaction_session_timeout"] == "123456"
    finally:
        settings.db_application_name = "oddish"
        settings.idle_in_transaction_session_timeout_ms = 300_000


@pytest.mark.asyncio
async def test_reaper_filters_by_application_name_and_age(monkeypatch):
    """The reaper only terminates our own backends older than N minutes."""

    executed_queries: list[tuple[str, dict]] = []

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeSession:
        async def execute(self, statement, params=None):
            executed_queries.append((str(statement), params or {}))
            return _FakeResult(
                [
                    SimpleNamespace(pid=1001, terminated=True),
                    SimpleNamespace(pid=1002, terminated=True),
                    SimpleNamespace(pid=1003, terminated=False),
                ]
            )

    @asynccontextmanager
    async def fake_get_session():
        yield _FakeSession()

    monkeypatch.setattr(cleanup_module, "get_session", fake_get_session)

    terminated = await cleanup_module.reap_idle_in_transaction_zombies(
        idle_after_minutes=7,
    )

    # Two of the three rows had terminated=True.
    assert terminated == 2

    assert len(executed_queries) == 1
    sql, params = executed_queries[0]
    assert "pg_terminate_backend" in sql
    assert "state = 'idle in transaction'" in sql
    assert "application_name = ANY(:app_names)" in sql
    assert "pid <> pg_backend_pid()" in sql
    assert params["idle_after_minutes"] == 7
    # Must include both our configured name and the pooler identity so it
    # works on direct Postgres AND through Supavisor.
    assert settings.db_application_name in params["app_names"]
    assert "Supavisor" in params["app_names"]


@pytest.mark.asyncio
async def test_reaper_swallows_permission_errors(monkeypatch):
    """If the role can't pg_terminate_backend (self-hosted, tests) we
    still return cleanly rather than breaking cleanup."""

    class _ExplodingSession:
        async def execute(self, *args, **kwargs):
            raise PermissionError("permission denied for function pg_terminate_backend")

    @asynccontextmanager
    async def fake_get_session():
        yield _ExplodingSession()

    monkeypatch.setattr(cleanup_module, "get_session", fake_get_session)

    assert await cleanup_module.reap_idle_in_transaction_zombies() == 0


@pytest.mark.asyncio
async def test_reaper_noop_when_allow_list_empty(monkeypatch):
    """With no app-name allow-list, we refuse to touch anything (too risky
    - we'd be matching every connection in the DB)."""

    original = settings.db_reaper_application_names
    settings.db_reaper_application_names = []
    try:
        called = False

        @asynccontextmanager
        async def fake_get_session():
            nonlocal called
            called = True
            raise AssertionError("should never open a session")
            yield  # pragma: no cover

        monkeypatch.setattr(cleanup_module, "get_session", fake_get_session)

        assert await cleanup_module.reap_idle_in_transaction_zombies() == 0
        assert not called
    finally:
        settings.db_reaper_application_names = original
