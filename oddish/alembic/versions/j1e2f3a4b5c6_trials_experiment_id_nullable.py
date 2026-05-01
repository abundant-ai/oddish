"""make trials.experiment_id nullable

P5 of the task-first migration. Trials no longer *belong to* an
experiment in the new model -- they're pinned to (task_version,
agent_equivalence_key) and grouped by Job. This migration relaxes the
NOT NULL so that when writers eventually stop populating it, existing
trials don't break.

This is an additive, no-behavior-change migration. The dual-write
introduced in P1 still populates ``experiment_id`` on every trial; the
follow-up migration that *drops* the column (and ``task_experiments``)
ships separately and should only be applied after a bake period during
which writers have been switched off.

Revision ID: j1e2f3a4b5c6
Revises: i0d1e2f3a4b5
Create Date: 2026-05-01 01:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "j1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "i0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ALTER COLUMN experiment_id DROP NOT NULL")


def downgrade() -> None:
    # Tighten back to NOT NULL only if every row has a value -- otherwise
    # the alter raises and the operator must backfill first.
    op.execute("ALTER TABLE trials ALTER COLUMN experiment_id SET NOT NULL")
