"""Static sanity checks for the trial_facets migration.

Mirrors ``test_tags_migration_sanity``: we can't run real DDL in the
unit-test container, so we read the migration file and assert the key SQL
fragments are present. Catches the easy regressions (dropped filter, missing
kind) without needing a Postgres instance.
"""

from __future__ import annotations

from pathlib import Path

from oddish.core.trial_facets import TRIAL_FACET_KINDS

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
SRC = (MIGRATIONS / "trial_facets_001_add_trial_facets.py").read_text()


def test_creates_table_with_composite_pk():
    assert "CREATE TABLE IF NOT EXISTS trial_facets" in SRC
    assert "PRIMARY KEY (org_id, kind, value, value_2)" in SRC


def test_backfill_covers_every_kind():
    for kind in TRIAL_FACET_KINDS:
        if kind == "agent_model":
            assert "'agent_model'" in SRC, "missing pair-kind backfill"
        else:
            assert f'("{kind}", ' in SRC, f"missing backfill for {kind}"


def test_backfill_matches_browse_population():
    # Raw SQL bypasses the ORM soft-delete listener, so every filter the
    # browse facets rely on must be explicit here.
    assert SRC.count("deleted_at IS NULL") >= 2  # trials AND tasks
    assert "t.is_probe IS NOT TRUE" in SRC
    assert "t.superseded_by_trial_id IS NULL" in SRC
    assert "t.task_version_id = k.current_version_id" in SRC
    assert "t.org_id IS NOT NULL" in SRC
    assert "ON CONFLICT DO NOTHING" in SRC


def test_downgrade_drops_table():
    assert "DROP TABLE IF EXISTS trial_facets" in SRC
