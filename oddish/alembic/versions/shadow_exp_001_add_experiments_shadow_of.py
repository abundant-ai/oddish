"""add experiments.shadow_of

Revision ID: shadow_exp_001
Revises: trial_kind_001
Create Date: 2026-08-07 00:00:00.000000

A shadow experiment holds a task's analysis trials (qa, audit) and points
at the experiment it grades. Shadows are hidden from experiment lists; the
graded experiment links to its shadow instead. The partial unique index
gives each live experiment at most one live shadow and lets the creator
use ON CONFLICT for a race-safe get-or-create.

Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "shadow_exp_001"
down_revision: Union[str, Sequence[str], None] = "trial_kind_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS shadow_of VARCHAR(64)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_experiments_shadow_of_live "
        "ON experiments (shadow_of) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_experiments_shadow_of_live")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS shadow_of")
