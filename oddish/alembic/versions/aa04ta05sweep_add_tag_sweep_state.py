"""tag-projection sweep-state singleton

A 1-row table holding the last time the hourly tag-projection reconciler
ran. Deliberately separate from tag_policies: tag_policies.org_id is an
org FK in cloud, so a sentinel marker row there would violate it.

Revision ID: aa04ta05sweep
Revises: aa02ta03gin
Create Date: 2026-06-06 12:25:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "aa04ta05sweep"
down_revision: Union[str, Sequence[str], None] = "aa02ta03gin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_projection_sweep_state (
            id                 BOOLEAN PRIMARY KEY DEFAULT TRUE,
            last_full_sweep_at TIMESTAMPTZ,
            CONSTRAINT tag_sweep_singleton CHECK (id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tag_projection_sweep_state")
