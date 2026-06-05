"""add_task_link

Adds tasks.link column for storing a URL associated with a task run
(e.g. PR, issue, or CI run).

Revision ID: a0b1c2d3e4f5
Revises: k2l3m4n5o6p7
Create Date: 2026-06-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS link TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS link")
