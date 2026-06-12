"""GIN(array_ops) indexes for effective_tag_ids on tasks and task_versions

The indexes serve the AND (``@>``), OR (``&&``), and NOT (``NOT (&&)``)
predicates that the browse filter uses. ``CREATE INDEX CONCURRENTLY``
cannot run inside a transaction, so the statements ride
``op.get_context().autocommit_block()`` (precedent:
``i1d2e3f4a5b6_partial_indexes_for_soft_delete``).

Revision ID: aa02ta03gin
Revises: aa01ta02proj
Create Date: 2026-06-06 12:10:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "aa02ta03gin"
down_revision: Union[str, Sequence[str], None] = "aa01ta02proj"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_tasks_effective_tag_ids_gin
            ON tasks
            USING GIN (effective_tag_ids array_ops)
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_task_versions_effective_tag_ids_gin
            ON task_versions
            USING GIN (effective_tag_ids array_ops)
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_task_versions_effective_tag_ids_gin"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_tasks_effective_tag_ids_gin"
        )
