"""add probe_scope to trials

Revision ID: p1r2o3b4s5c6
Revises: mine_filter_idx_001
Create Date: 2026-06-10 12:00:00.000000

Adds a nullable, indexed ``probe_scope`` string to ``trials`` describing the
scope of a probe (e.g. ``task``). Lives in the oddish migration tree
(``alembic_version_oddish``).

Idempotent: ``000_initial_schema`` runs ``Base.metadata.create_all()``, so on a
fresh DB the column + ``ix_trials_probe_scope`` index already exist by the time
this runs. The ``if_not_exists`` guards make the upgrade a no-op there while
still adding the column/index on existing prod DBs.

Backfill: existing probe rows predate this column and were all task-scoped, so
mark them ``'task'`` (no-op on a fresh DB).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "p1r2o3b4s5c6"
down_revision: Union[str, Sequence[str], None] = "mine_filter_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trials",
        sa.Column("probe_scope", sa.String(length=16), nullable=True),
        if_not_exists=True,
    )
    # Backfill: existing probes were all task-scoped. No-op on a fresh DB.
    op.execute(
        "UPDATE trials SET probe_scope = 'task' "
        "WHERE is_probe = true AND probe_scope IS NULL"
    )
    # Index name matches SQLAlchemy's default for ``index=True`` (ix_<table>_<col>)
    # so the model's create_all() index and this one are the same object.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_trials_probe_scope ON trials (probe_scope)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_trials_probe_scope")
    op.drop_column("trials", "probe_scope", if_exists=True)
