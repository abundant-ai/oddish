"""make_tasks_org_name_unique_partial_on_soft_delete

Revision ID: h9c0d1e2f3a4
Revises: g8b9c0d1e2f3
Create Date: 2026-05-11 14:00:00.000000

Rebuilds ``idx_tasks_unique_org_name`` as a partial unique index that
only constrains live (``deleted_at IS NULL``) rows. Soft-deleting a
task now leaves its name slot free for a future re-creation under the
same ``(org_id, name)`` pair -- without this change the tombstoned
row's name keeps the slot reserved and breaks normal re-upload flows.

The migration is wrapped in a transactional rebuild because Postgres
won't let us add a ``WHERE`` clause to an existing index in place. We
keep the same index *name* (``idx_tasks_unique_org_name``) so reads of
``pg_indexes`` / ``pg_stat_user_indexes`` line up across deployments.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "h9c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "g8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tasks_unique_org_name")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_tasks_unique_org_name
        ON tasks (COALESCE(org_id, ''), name)
        WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tasks_unique_org_name")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_tasks_unique_org_name
        ON tasks (COALESCE(org_id, ''), name)
        """
    )
