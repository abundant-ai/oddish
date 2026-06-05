"""add_task_link

Adds tasks.link column for storing a URL associated with a task run
(e.g. PR, issue, or CI run).

Revision ID: l8m9n0p1q2r3
Revises: k7l8m9n0p1q2
Create Date: 2026-06-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "l8m9n0p1q2r3"
down_revision: Union[str, Sequence[str], None] = "k7l8m9n0p1q2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS link TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS link")
