from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _upgrade_sql() -> str:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/expownercreate001_backfill_creation_owner.py"
    )
    spec = importlib.util.spec_from_file_location(
        "experiment_owner_backfill", migration_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    statements: list[str] = []
    original_execute = module.op.execute
    module.op.execute = statements.append
    try:
        module.upgrade()
    finally:
        module.op.execute = original_execute

    assert len(statements) == 1
    return statements[0]


def test_backfill_uses_the_exact_first_trial_including_history() -> None:
    sql = _upgrade_sql()
    assert "DISTINCT ON (t.experiment_id)" in sql
    assert "ORDER BY t.experiment_id, t.created_at ASC, t.id ASC" in sql
    assert "owner_user_id IS NULL" in sql
    assert "t.deleted_at" not in sql
    assert "superseded_by_trial_id" not in sql


def test_backfill_only_accepts_a_first_trial_payer_from_the_experiment_org() -> None:
    sql = _upgrade_sql()

    # Validate only after selecting the exact first trial. Joining users inside
    # the CTE would silently fall forward to a later trial when the first trial
    # has stale or cross-tenant billing attribution.
    first_trial_query, update_query = sql.split("UPDATE experiments AS e", maxsplit=1)
    assert "users" not in first_trial_query
    assert "JOIN users AS billed_user" in update_query
    assert "billed_user.id = earliest_trial.billed_user_id" in update_query
    assert "earliest_trial.org_id = e.org_id" in update_query
    assert "billed_user.org_id = e.org_id" in update_query


@requires_db
@pytest.mark.asyncio
async def test_backfill_leaves_cross_org_attribution_unowned() -> None:
    assert DB_URL is not None
    engine = create_async_engine(DB_URL)
    try:
        async with engine.begin() as conn:
            # Temporary tables shadow the real schema for this connection, so
            # this runs the migration SQL without mutating shared test data.
            statements = (
                """
                CREATE TEMP TABLE experiments (
                        id TEXT PRIMARY KEY,
                        org_id TEXT NOT NULL,
                        owner_user_id TEXT
                )
                """,
                """
                CREATE TEMP TABLE trials (
                        id TEXT PRIMARY KEY,
                        experiment_id TEXT,
                        org_id TEXT NOT NULL,
                        billed_user_id TEXT,
                        created_at TIMESTAMPTZ NOT NULL
                )
                """,
                """
                CREATE TEMP TABLE users (
                        id TEXT PRIMARY KEY,
                        org_id TEXT NOT NULL
                )
                """,
                """
                INSERT INTO users (id, org_id) VALUES
                    ('user-a', 'org-a'),
                    ('user-b', 'org-b')
                """,
                """
                INSERT INTO experiments (id, org_id) VALUES
                    ('valid', 'org-a'),
                    ('foreign-payer', 'org-a'),
                    ('foreign-trial', 'org-a'),
                    ('no-fall-forward', 'org-a')
                """,
                """
                INSERT INTO trials (
                    id, experiment_id, org_id, billed_user_id, created_at
                ) VALUES
                    ('t-valid', 'valid', 'org-a', 'user-a', '2026-01-01'),
                    ('t-payer', 'foreign-payer', 'org-a', 'user-b', '2026-01-01'),
                    ('t-trial', 'foreign-trial', 'org-b', 'user-a', '2026-01-01'),
                    ('t-first', 'no-fall-forward', 'org-a', 'user-b', '2026-01-01'),
                    ('t-later', 'no-fall-forward', 'org-a', 'user-a', '2026-01-02')
                """,
            )
            for statement in statements:
                await conn.execute(text(statement))
            await conn.execute(text(_upgrade_sql()))
            result = await conn.execute(
                text("SELECT id, owner_user_id FROM experiments ORDER BY id")
            )

        assert dict(result.all()) == {
            "foreign-payer": None,
            "foreign-trial": None,
            "no-fall-forward": None,
            "valid": "user-a",
        }
    finally:
        await engine.dispose()
