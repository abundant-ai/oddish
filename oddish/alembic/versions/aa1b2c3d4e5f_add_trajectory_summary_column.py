"""add_trajectory_summary_column

Revision ID: aa1b2c3d4e5f
Revises: e6f7a8b9c0d1
Create Date: 2026-05-02 12:00:00.000000

Adds a JSONB column on trials to store the LLM-generated trajectory
summary, replacing the prior S3-cached sibling file.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "aa1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS trajectory_summary JSONB"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS trajectory_summary")
