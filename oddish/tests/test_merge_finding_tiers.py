"""Exercise the real severity migration without rewriting finding evidence."""

import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text


@pytest.mark.asyncio
async def test_migration_converts_stored_findings_and_older_worker_writes(session):
    spec = importlib.util.spec_from_file_location(
        "merge_finding_tiers",
        Path(__file__).parents[1] / "alembic/versions/merge_finding_tiers_001.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    await session.execute(text("CREATE SCHEMA merge_finding_tiers_test"))
    await session.execute(text("SET LOCAL search_path TO merge_finding_tiers_test"))
    columns = {
        "task_versions": ("pre_trial", "reported_findings"),
        "trials": ("analysis",),
        "tasks": ("verdict",),
        "delivery_snapshots": ("snapshot",),
    }
    original = {
        "items": [{"id": "stable-id", "tier": "should_fix", "detail": "should_fix"}],
        "severity": "should_fix",
        "priority": "should_fix",
        "recorded_tier": "should_fix",
        "acknowledged": True,
    }
    converted = {
        **original,
        "items": [{**original["items"][0], "tier": "must_fix"}],
        "severity": "must_fix",
        "priority": "must_fix",
        "recorded_tier": "must_fix",
    }
    for table, fields in columns.items():
        await session.execute(
            text(
                f"CREATE TABLE {table} ("
                + ", ".join(f"{field} jsonb" for field in fields)
                + ")"
            )
        )
        await session.execute(
            text(
                f"INSERT INTO {table} VALUES ("
                + ", ".join("CAST(:payload AS jsonb)" for _ in fields)
                + ")"
            ),
            {"payload": json.dumps(original)},
        )

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    await (await session.connection()).run_sync(upgrade)
    for table, fields in columns.items():
        for field in fields:
            assert (
                await session.scalar(text(f"SELECT {field} FROM {table}")) == converted
            )
    await session.execute(
        text("UPDATE trials SET analysis=CAST(:payload AS jsonb)"),
        {"payload": json.dumps(original)},
    )
    assert await session.scalar(text("SELECT analysis FROM trials")) == converted
