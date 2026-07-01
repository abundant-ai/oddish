"""add imported_at (tasks/trials/experiments) + orig_s3_src (trials/experiments)

Supports the Sauron->Oddish migration. ``imported_at`` is a nullable timestamp
stamped only on rows created by the legacy importer -- NULL for every normal
row -- giving a clean audit/rollback marker (``WHERE imported_at IS NOT NULL``).
``orig_s3_src`` is the immutable Sauron S3 path a row came from (the run root for
experiments, the attempt prefix for trials); it encodes base/pr/run and is the
anchor for later cross-org duplicate reconciliation. Tasks dedup across many
source paths, so their legacy source list stays in the existing ``tags`` JSONB.

All additive + nullable: no backfill, no rewrite of existing rows, safe on a
live table. Partial indexes on ``imported_at`` keep audit/rollback scans cheap
without indexing the (overwhelmingly NULL) native rows.

Revision ID: legacy_imported_at_001
Revises: merge_mca01_wjtoken_heads
Create Date: 2026-07-01 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "legacy_imported_at_001"
down_revision: Union[str, Sequence[str], None] = "merge_mca01_wjtoken_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks       ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ")
    op.execute("ALTER TABLE trials      ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ")
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ")
    op.execute("ALTER TABLE trials      ADD COLUMN IF NOT EXISTS orig_s3_src TEXT")
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS orig_s3_src TEXT")

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_imported_at "
        "ON tasks (imported_at) WHERE imported_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_imported_at "
        "ON trials (imported_at) WHERE imported_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiments_imported_at "
        "ON experiments (imported_at) WHERE imported_at IS NOT NULL"
    )
    # Anchor for dedup reconciliation / lookup by source path.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_orig_s3_src "
        "ON trials (orig_s3_src) WHERE orig_s3_src IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_trials_orig_s3_src")
    op.execute("DROP INDEX IF EXISTS idx_tasks_imported_at")
    op.execute("DROP INDEX IF EXISTS idx_trials_imported_at")
    op.execute("DROP INDEX IF EXISTS idx_experiments_imported_at")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS orig_s3_src")
    op.execute("ALTER TABLE trials      DROP COLUMN IF EXISTS orig_s3_src")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS imported_at")
    op.execute("ALTER TABLE trials      DROP COLUMN IF EXISTS imported_at")
    op.execute("ALTER TABLE tasks       DROP COLUMN IF EXISTS imported_at")
