"""Static sanity checks for the model_concurrency_advisory migration.

Mirrors test_tags_migration_sanity: read the migration file and assert the
revision chains onto the current oddish head, that the table is created
idempotently with the expected columns, and that adding it keeps a single head
(a renamed/mis-chained revision would strand prod's alembic pointer).
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIG = "mca01_add_model_concurrency_advisory.py"
REVISION = "mca01_model_concurrency_advisory"
DOWN = "r8a9drop_probe_ratio_001"


def _read(name: str) -> str:
    return (MIGRATIONS / name).read_text()


def _down_revision_of(src: str) -> str | None:
    m = re.search(r'down_revision[^=]*=\s*"([^"]*)"', src)
    return m.group(1) if m else None


def test_revision_chains_onto_current_head():
    src = _read(MIG)
    assert f'revision: str = "{REVISION}"' in src
    assert _down_revision_of(src) == DOWN


def test_creates_advisory_table_idempotently():
    src = _read(MIG)
    assert "CREATE TABLE IF NOT EXISTS model_concurrency_advisory" in src
    assert "queue_key" in src and "TEXT PRIMARY KEY" in src
    assert "advisory_limit" in src and "INTEGER NOT NULL" in src
    assert "TIMESTAMPTZ NOT NULL DEFAULT NOW()" in src
    assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in src


def test_downgrade_drops_table():
    assert "DROP TABLE IF EXISTS model_concurrency_advisory" in _read(MIG)


def test_adding_migration_keeps_single_head():
    # No other migration may chain onto this revision (it must be the new head),
    # and exactly one migration may chain onto the prior head (this one).
    down_revisions = []
    for path in MIGRATIONS.glob("*.py"):
        down_revisions.append(_down_revision_of(path.read_text()))
    assert REVISION not in down_revisions, "another migration already chains onto mca01"
    assert down_revisions.count(DOWN) == 1, (
        f"expected exactly one migration to chain onto {DOWN}; the prior head "
        "must not have grown a second child"
    )
