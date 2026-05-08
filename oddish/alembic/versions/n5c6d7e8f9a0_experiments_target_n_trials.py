"""add experiments.target_n_trials

Adds an experiment-level trial-target column. Cells still own their
own ``target_n_trials`` (so per-cell overrides stay possible), but
this column is what the UI displays and edits as "trials per cell"
for the experiment as a whole. New cells default to it; the bulk
"bump all targets" op rewrites both this column and every cell.

Revision ID: n5c6d7e8f9a0
Revises: m4b5c6d7e8f9
Create Date: 2026-05-02 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "n5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "m4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE experiments
            ADD COLUMN IF NOT EXISTS target_n_trials INTEGER NOT NULL DEFAULT 3
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS target_n_trials")
