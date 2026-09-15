"""Retired severities cannot survive migration or an older writer."""

import importlib.util
import json
from pathlib import Path

import pytest
import pytest_asyncio
from uuid import uuid4
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, text
from sqlalchemy.util.concurrency import await_only

from oddish.analyze.models import ActionTier


def test_retired_category_is_not_an_accepted_tier():
    assert [tier.value for tier in ActionTier] == ["must_fix", "optional"]
    with pytest.raises(ValueError):
        ActionTier("should_fix")


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location(
        "merge_finding_tiers",
        Path(__file__).parents[1] / "alembic/versions/merge_finding_tiers_001.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def migration_database(migration):
    from oddish.db.connection import engine

    # Autocommit migrations need a real committed schema, not the session
    # fixture's rollback-only transaction. Each test owns and removes its schema.
    schema = f"finding_migration_{uuid4().hex}"
    async with engine.connect() as connection, engine.connect() as writer:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
        await connection.execute(text(f"SET search_path TO {schema}"))
        for table, columns in migration.FINDING_COLUMNS.items():
            await connection.execute(
                text(
                    f"CREATE TABLE {table} (id text PRIMARY KEY, "
                    + ", ".join(f"{column} jsonb" for column in columns)
                    + ")"
                )
            )
        await connection.commit()
        await writer.execute(text(f"SET search_path TO {schema}"))
        await writer.execute(text("SET lock_timeout = '500ms'"))
        await writer.commit()
        try:
            yield connection, writer
        finally:
            await writer.rollback()
            await connection.rollback()
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            await connection.commit()


def run_upgrade(connection, migration):
    context = MigrationContext.configure(connection)
    with context.begin_transaction(), Operations.context(context):
        migration.upgrade()


@pytest.mark.asyncio
async def test_migration_converts_all_finding_storage_and_normalizes_future_writes(
    migration_database,
    migration,
):
    session, _ = migration_database
    original = {
        "items": [
            {
                "id": "stable-id",
                "tier": "should_fix",
                "detail": "should_fix is quoted evidence",
            },
            {"severity": "should_fix", "line_start": 7},
            {"tier": "optional"},
            {"tier": "must_fix"},
            None,
        ],
        "recommendations": [{"priority": "should_fix", "action": "Keep this action"}],
        "retained": {"recorded_tier": "should_fix", "finding": {"tier": "should_fix"}},
        "pre_trial_should_fix": 2,
        "acknowledged": True,
        "check_key": "ack:stable-id",
        "verdict": "accept",
        "empty": [],
        "empty_object": {},
    }
    converted = {
        **original,
        "items": [
            {
                "id": "stable-id",
                "tier": "must_fix",
                "detail": "should_fix is quoted evidence",
            },
            {"severity": "must_fix", "line_start": 7},
            {"tier": "optional"},
            {"tier": "must_fix"},
            None,
        ],
        "recommendations": [{"priority": "must_fix", "action": "Keep this action"}],
        "retained": {"recorded_tier": "must_fix", "finding": {"tier": "must_fix"}},
    }
    del converted["pre_trial_should_fix"]
    for table, columns in migration.FINDING_COLUMNS.items():
        await session.execute(
            text(
                f"INSERT INTO {table} VALUES ('1', "
                + ", ".join("CAST(:payload AS jsonb)" for _ in columns)
                + ")"
            ),
            {"payload": json.dumps(original)},
        )
        await session.execute(text(f"INSERT INTO {table} (id) VALUES ('2')"))

    await session.commit()
    await session.run_sync(run_upgrade, migration)
    for table, columns in migration.FINDING_COLUMNS.items():
        for column in columns:
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id='1'"))
                == converted
            )
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id='2'"))
                is None
            )
            # A stale worker or imported report cannot restore the category.
            await session.execute(
                text(
                    f"UPDATE {table} SET {column}=CAST(:payload AS jsonb) WHERE id='2'"
                ),
                {"payload": json.dumps(original)},
            )
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id='2'"))
                == converted
            )
        await session.execute(
            text(
                f"INSERT INTO {table} VALUES ('3', "
                + ", ".join("CAST(:payload AS jsonb)" for _ in columns)
                + ")"
            ),
            {"payload": json.dumps(original)},
        )
        for column in columns:
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id='3'"))
                == converted
            )
    assert (
        await session.scalar(
            text(
                "SELECT normalize_finding_tiers(normalize_finding_tiers(CAST(:payload AS jsonb)))"
            ),
            {"payload": json.dumps(original)},
        )
        == converted
    )


@pytest.mark.asyncio
async def test_history_batches_release_locks_and_allow_concurrent_writes(
    migration_database,
    migration,
    monkeypatch,
):
    connection, writer = migration_database
    monkeypatch.setattr(migration, "BATCH_SIZE", 2, raising=False)
    for table, columns in migration.FINDING_COLUMNS.items():
        for index in range(5):
            # Clean rows must advance the cursor too, including a clean batch.
            payload = '{"tier":"should_fix"}' if index in {0, 4} else "{}"
            await connection.execute(
                text(
                    f"INSERT INTO {table} VALUES (:id, "
                    + ", ".join("CAST(:payload AS jsonb)" for _ in columns)
                    + ")"
                ),
                {"id": str(index), "payload": payload},
            )
    await connection.commit()
    checked = False

    async def write_during_conversion():
        for table, columns in migration.FINDING_COLUMNS.items():
            # All table triggers must already be committed before any history
            # scan. This ID sorts behind the cursor and cannot rely on backfill.
            await writer.execute(
                text(
                    f"INSERT INTO {table} VALUES ('-concurrent', "
                    + ", ".join("CAST(:payload AS jsonb)" for _ in columns)
                    + ")"
                ),
                {"payload": '{"tier":"should_fix"}'},
            )
            for column in columns:
                assert (
                    await writer.scalar(
                        text(
                            f"SELECT {column}->>'tier' FROM {table} WHERE id='-concurrent'"
                        )
                    )
                    == "must_fix"
                )
            # A completed history batch must not keep row locks either.
            await writer.execute(text(f"UPDATE {table} SET id=id WHERE id='0'"))
        await writer.commit()

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        nonlocal checked
        if not checked and "UPDATE trials" in statement:
            checked = True
            await_only(write_during_conversion())

    event.listen(connection.sync_connection, "before_cursor_execute", before_sql)
    try:
        await connection.run_sync(run_upgrade, migration)
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", before_sql)
    assert checked
    for table, columns in migration.FINDING_COLUMNS.items():
        for column in columns:
            assert (
                await writer.scalar(
                    text(f"SELECT {column}->>'tier' FROM {table} WHERE id='4'")
                )
                == "must_fix"
            )


@pytest.mark.asyncio
async def test_interrupted_backfill_can_be_retried(
    migration_database, migration, monkeypatch
):
    connection, writer = migration_database
    monkeypatch.setattr(migration, "BATCH_SIZE", 2, raising=False)
    await connection.execute(
        text(
            """
        INSERT INTO task_versions (id, pre_trial)
        SELECT n::text, '{"tier":"should_fix"}'::jsonb FROM generate_series(1, 5) n
    """
        )
    )
    await connection.execute(text("SET lock_timeout = '7s'"))
    await connection.commit()

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if "WITH batch AS MATERIALIZED" in statement:
            raise RuntimeError("injected interruption after committed batch")

    event.listen(connection.sync_connection, "after_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="injected interruption"):
            await connection.run_sync(run_upgrade, migration)
    finally:
        event.remove(connection.sync_connection, "after_cursor_execute", interrupt)
    assert await connection.scalar(text("SHOW lock_timeout")) == "7s"
    assert (
        await writer.scalar(
            text(
                """
        SELECT count(*) FROM task_versions WHERE pre_trial->>'tier' = 'must_fix'
    """
            )
        )
        == 2
    )
    await connection.commit()
    await connection.run_sync(run_upgrade, migration)
    assert (
        await writer.scalar(
            text(
                """
        SELECT count(*) FROM task_versions WHERE pre_trial->>'tier' = 'must_fix'
    """
            )
        )
        == 5
    )
    await connection.commit()
    # A repeat also works once everything is converted (no duplicate DDL).
    await connection.run_sync(run_upgrade, migration)
    assert await connection.scalar(text("SHOW lock_timeout")) == "7s"


@pytest.mark.asyncio
async def test_busy_table_times_out_without_leaving_session_settings_changed(
    migration_database,
    migration,
):
    connection, writer = migration_database
    await connection.execute(text("SET lock_timeout = '7s'"))
    await connection.commit()
    # An existing writer prevents CREATE TRIGGER from acquiring its table lock.
    await writer.execute(text("INSERT INTO task_versions (id) VALUES ('busy')"))
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="lock timeout"):
        await connection.run_sync(run_upgrade, migration)
    assert await connection.scalar(text("SHOW lock_timeout")) == "7s"
    await writer.rollback()
    await connection.commit()
    await connection.run_sync(run_upgrade, migration)
