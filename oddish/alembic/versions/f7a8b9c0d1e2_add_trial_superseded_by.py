"""add_trial_superseded_by

Adds the ``superseded_by_trial_id`` pointer that turns trials into
immutable units. When a user retries a trial we no longer reset the
existing row; instead we insert a fresh trial copying its spec, mark
the old row by pointing ``superseded_by_trial_id`` at the new one,
and filter superseded rows out of default UI listings, pipeline
counts, and verdict aggregation.

The column is nullable -- a NULL value means "this is the latest
attempt in its chain" (the row that should be visible to listings).
ON DELETE SET NULL keeps history navigation working even if the
forward pointer's target is later removed.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-05-11 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE trials
        ADD COLUMN IF NOT EXISTS superseded_by_trial_id VARCHAR(128)
        """
    )
    # FK is added separately so re-running the migration after a
    # partial failure is safe (CockroachDB / older Postgres don't
    # support ``ADD CONSTRAINT IF NOT EXISTS``; this just no-ops if
    # the constraint already exists).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'trials_superseded_by_trial_id_fkey'
            ) THEN
                ALTER TABLE trials
                ADD CONSTRAINT trials_superseded_by_trial_id_fkey
                FOREIGN KEY (superseded_by_trial_id)
                REFERENCES trials(id)
                ON DELETE SET NULL;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_superseded_by
        ON trials (superseded_by_trial_id)
        WHERE superseded_by_trial_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_trials_superseded_by")
    op.execute(
        "ALTER TABLE trials DROP CONSTRAINT IF EXISTS "
        "trials_superseded_by_trial_id_fkey"
    )
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS superseded_by_trial_id")
