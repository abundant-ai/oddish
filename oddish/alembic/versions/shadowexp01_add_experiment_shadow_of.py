"""add shadow_of column to experiments

Revision ID: shadowexp01
Revises: trialkind01
Create Date: 2026-08-18 00:00:00.000000

Adds ``experiments.shadow_of``: NULL for normal experiments; set to another
experiment's id on the hidden "shadow" experiment that will home that
experiment's analysis trials (qa/audit) once the analysis-trial pipeline
lands. The partial unique index enforces one *live* shadow per experiment and
lets the shadow creator use ``INSERT .. ON CONFLICT`` for a race-safe
get-or-create; a soft-deleted shadow frees the slot. Nothing writes the
column yet.

Idempotent: ``000_initial_schema`` runs ``Base.metadata.create_all()``, so on
a fresh DB the column + index already exist by the time this runs. The
``if_not_exists`` guards make the upgrade a no-op there while still adding
the column/index on existing prod DBs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.db.migration_locks import run_with_lock_retry

revision: str = "shadowexp01"
down_revision: Union[str, Sequence[str], None] = "trialkind01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _recover_invalid_index(index_name: str) -> None:
    """An interrupted ``CREATE INDEX CONCURRENTLY`` leaves a same-name INVALID
    index behind (``pg_index.indisvalid = false``). ``IF NOT EXISTS`` sees that
    relation and skips the CREATE, so a retried migration would complete with
    an index that enforces nothing -- for this unique index that silently
    breaks the shadow creator's ``INSERT .. ON CONFLICT`` arbiter inference,
    which only considers valid indexes. Drop the invalid leftover
    (concurrently, we are in the autocommit block) so the CREATE below
    rebuilds it."""
    result = op.get_bind().execute(
        sa.text(
            """
            SELECT 1
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = :index_name
              AND n.nspname = current_schema()
              AND NOT i.indisvalid
            """
        ),
        {"index_name": index_name},
    )
    invalid = result.first()
    if invalid is not None:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


def upgrade() -> None:
    # The ALTER's ACCESS EXCLUSIVE lock is bounded by a short lock_timeout
    # (an uncapped wait queues all traffic on ``experiments`` behind it) and
    # retried, since one short window can lose to in-flight queries.
    run_with_lock_retry(
        lambda: op.add_column(
            "experiments",
            sa.Column("shadow_of", sa.String(64), nullable=True),
            if_not_exists=True,
        ),
        table_name="experiments",
    )

    # Index name matches the model's ``__table_args__`` declaration so the
    # create_all() index and this one are the same object. The invalid-index
    # recovery runs inside the retried step: a lock-timed-out CREATE INDEX
    # CONCURRENTLY leaves an INVALID index the next attempt must drop first.
    def _create_index() -> None:
        _recover_invalid_index("uq_experiments_shadow_of_live")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "uq_experiments_shadow_of_live ON experiments (shadow_of) "
            "WHERE deleted_at IS NULL"
        )

    run_with_lock_retry(_create_index, table_name="experiments")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_experiments_shadow_of_live")
    op.execute("SET lock_timeout = '8s'")
    op.drop_column("experiments", "shadow_of", if_exists=True)
