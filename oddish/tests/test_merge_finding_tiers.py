"""Retired severities cannot survive migration or an older writer."""

import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from oddish.analyze.models import ActionTier


def test_retired_category_is_not_an_accepted_tier():
    assert [tier.value for tier in ActionTier] == ["must_fix", "optional"]
    with pytest.raises(ValueError):
        ActionTier("should_fix")


@pytest.mark.asyncio
async def test_migration_converts_all_finding_storage_and_normalizes_future_writes(
    session,
):
    spec = importlib.util.spec_from_file_location(
        "merge_finding_tiers",
        Path(__file__).parents[1] / "alembic/versions/merge_finding_tiers_001.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    await session.execute(text("CREATE SCHEMA merge_finding_tiers_test"))
    await session.execute(text("SET LOCAL search_path TO merge_finding_tiers_test"))
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
                f"CREATE TABLE {table} (id int PRIMARY KEY, "
                + ", ".join(f"{c} jsonb" for c in columns)
                + ")"
            )
        )
        await session.execute(
            text(
                f"INSERT INTO {table} VALUES (1, "
                + ", ".join("CAST(:payload AS jsonb)" for _ in columns)
                + ")"
            ),
            {"payload": json.dumps(original)},
        )
        await session.execute(text(f"INSERT INTO {table} (id) VALUES (2)"))

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    await (await session.connection()).run_sync(upgrade)
    for table, columns in migration.FINDING_COLUMNS.items():
        for column in columns:
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id=1"))
                == converted
            )
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id=2"))
                is None
            )
            # A stale worker or imported report cannot restore the category.
            await session.execute(
                text(f"UPDATE {table} SET {column}=CAST(:payload AS jsonb) WHERE id=2"),
                {"payload": json.dumps(original)},
            )
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id=2"))
                == converted
            )
        await session.execute(
            text(
                f"INSERT INTO {table} VALUES (3, "
                + ", ".join("CAST(:payload AS jsonb)" for _ in columns)
                + ")"
            ),
            {"payload": json.dumps(original)},
        )
        for column in columns:
            assert (
                await session.scalar(text(f"SELECT {column} FROM {table} WHERE id=3"))
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
