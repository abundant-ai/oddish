"""add effective_tag_ids projection columns

Adds the materialized projection arrays that ``oddish.core.tags_projection``
recomputes from truth. ``TEXT[] NOT NULL DEFAULT '{}'`` with a constant
default is a metadata-only ALTER on PG11+ so no table rewrite occurs.

Revision ID: aa01ta02proj
Revises: aa00ta01core
Create Date: 2026-06-06 12:05:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "aa01ta02proj"
down_revision: Union[str, Sequence[str], None] = "aa00ta01core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS effective_tag_ids "
        "TEXT[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS current_version_tag_ids "
        "TEXT[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE task_versions ADD COLUMN IF NOT EXISTS effective_tag_ids "
        "TEXT[] NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS current_version_tag_ids")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS effective_tag_ids")
    op.execute("ALTER TABLE task_versions DROP COLUMN IF EXISTS effective_tag_ids")
