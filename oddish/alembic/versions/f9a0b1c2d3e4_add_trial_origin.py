"""add trials.origin column

Adds a ``trials.origin`` column so we can distinguish trials that ran on
Oddish's worker runtime from trials that were executed elsewhere (e.g.
locally via ``harbor``) and uploaded via ``oddish import`` /
``/trials/import/*``.

The column is NOT NULL with a default of ``'oddish'`` so existing rows
are backfilled in place without a two-step rollout. A simple CHECK
constraint keeps the values restricted to the known set while we stay
on the ``VARCHAR`` representation (matching the ``native_enum=False``
choice in ``TrialModel.origin``).

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-04-21 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE trials
        ADD COLUMN IF NOT EXISTS origin VARCHAR(16)
            NOT NULL DEFAULT 'oddish'
        """
    )
    op.execute(
        """
        ALTER TABLE trials
        DROP CONSTRAINT IF EXISTS trials_origin_check
        """
    )
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT trials_origin_check
        CHECK (origin IN ('oddish', 'imported'))
        """
    )
    # Supports experiment/dashboard filters like "imported trials only"
    # without a full-table scan on the org_id index.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_origin
        ON trials (origin)
        WHERE origin <> 'oddish'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_trials_origin")
    op.execute(
        "ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_origin_check"
    )
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS origin")
