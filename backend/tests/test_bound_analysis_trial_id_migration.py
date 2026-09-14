"""PostgreSQL coverage for the bound analysis trial ID width migration."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

DATABASE_URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="ODDISH_DATABASE_URL not set")

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "boundanalysis160_widen_bound_trial_id.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("boundanalysis160", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_migration_accepts_full_length_trial_id() -> None:
    assert DATABASE_URL is not None
    database_url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    migration = _load_migration()
    engine = create_async_engine(database_url, poolclass=NullPool)

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TEMPORARY TABLE api_keys ("
                    "bound_analysis_trial_id VARCHAR(64)"
                    ") ON COMMIT DROP"
                )
            )

            def run_upgrade(sync_connection) -> None:
                context = MigrationContext.configure(sync_connection)
                with Operations.context(context):
                    migration.upgrade()

            await connection.run_sync(run_upgrade)
            maximum_length = await connection.scalar(
                text(
                    "SELECT character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema LIKE 'pg_temp_%' "
                    "AND table_name = 'api_keys' "
                    "AND column_name = 'bound_analysis_trial_id'"
                )
            )
            assert maximum_length == 160

            trial_id = "q" * 160
            await connection.execute(
                text(
                    "INSERT INTO api_keys (bound_analysis_trial_id) VALUES (:trial_id)"
                ),
                {"trial_id": trial_id},
            )
            stored_id = await connection.scalar(
                text("SELECT bound_analysis_trial_id FROM api_keys")
            )
            assert stored_id == trial_id
    finally:
        await engine.dispose()
