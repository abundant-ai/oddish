"""Real PostgreSQL checks for pooled reads, connection recovery, and writes."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text

from oddish.db import connection


@pytest_asyncio.fixture
async def pooled_database(monkeypatch):
    commands = []

    class RecordedConnection(asyncpg.Connection):
        async def execute(self, query, *args, **kwargs):
            commands.append(query)
            return await super().execute(query, *args, **kwargs)

        async def fetchrow(self, query, *args, **kwargs):
            commands.append(query)
            return await super().fetchrow(query, *args, **kwargs)

    connect_args = connection._base_connect_args()
    connect_args["connection_class"] = RecordedConnection
    monkeypatch.setattr(connection, "_base_connect_args", lambda: connect_args)
    monkeypatch.setattr(type(connection.settings), "db_use_null_pool", False)
    monkeypatch.setattr(type(connection.settings), "db_pool_size", 1)
    monkeypatch.setattr(type(connection.settings), "db_pool_max_overflow", 0)
    engine = connection._create_engine()
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(
        connection, "async_session_maker", connection._create_session_maker(engine)
    )
    try:
        yield commands
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reused_read_connection_pings_without_transaction(pooled_database):
    async with connection.get_read_session() as session:
        original_pid = await session.scalar(text("SELECT pg_backend_pid()"))
    pooled_database.clear()

    async with connection.get_read_session() as session:
        assert await session.scalar(text("SELECT pg_backend_pid()")) == original_pid
        first = await session.scalar(text("SELECT txid_current()"))
        second = await session.scalar(text("SELECT txid_current()"))
    assert first != second
    assert pooled_database == [";"]


@pytest.mark.asyncio
async def test_reused_write_connection_commits_and_rolls_back(pooled_database):
    async with connection.engine.begin() as conn:
        await conn.execute(text("CREATE TEMP TABLE atif_write_check (value integer)"))
    pooled_database.clear()
    async with connection.get_session() as session:
        await session.execute(text("INSERT INTO atif_write_check VALUES (1)"))
        first = await session.scalar(text("SELECT txid_current()"))
        assert await session.scalar(text("SELECT txid_current()")) == first
    assert pooled_database == [";", "BEGIN ISOLATION LEVEL READ COMMITTED;", "COMMIT;"]

    pooled_database.clear()
    with pytest.raises(RuntimeError, match="abort write"):
        async with connection.get_session() as session:
            await session.execute(text("INSERT INTO atif_write_check VALUES (2)"))
            raise RuntimeError("abort write")
    assert pooled_database == [
        ";",
        "BEGIN ISOLATION LEVEL READ COMMITTED;",
        "ROLLBACK;",
    ]

    pooled_database.clear()
    async with connection.get_read_session() as session:
        assert (
            await session.scalars(text("SELECT value FROM atif_write_check"))
        ).all() == [1]
    assert pooled_database == [";"]


@pytest.mark.asyncio
async def test_dead_pooled_connection_is_replaced(pooled_database):
    async with connection.get_read_session() as session:
        original_pid = await session.scalar(text("SELECT pg_backend_pid()"))
    # A separate admin connection kills only the backend opened by this test.
    admin = await asyncpg.connect(
        connection.db_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        assert await admin.fetchval("SELECT pg_terminate_backend($1)", original_pid)
    finally:
        await admin.close()
    async with connection.get_read_session() as session:
        assert await session.scalar(text("SELECT pg_backend_pid()")) != original_pid
        assert await session.scalar(text("SELECT 42")) == 42
