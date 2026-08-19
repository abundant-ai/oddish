"""add kind column to trials

Revision ID: trialkind01
Revises: dropanalyzers01
Create Date: 2026-08-18 00:00:00.000000

Adds ``trials.kind``: ``'agent'`` (the default) is a normal evaluation run;
any other value is a platform analysis agent run (``'qa'`` / ``'audit'``
arrive with the analysis-trial pipeline). Nothing writes a non-agent value
yet -- the column lands first so every counter/summer can become kind-aware
before the writers exist. The partial index only holds the (rare) non-agent
rows and serves both the user-facing exclusion filters and the QA-cost
surfaces' ``kind != 'agent'`` selections.

Idempotent: ``000_initial_schema`` runs ``Base.metadata.create_all()``, so on
a fresh DB the column + index already exist by the time this runs. The
``if_not_exists`` guards make the upgrade a no-op there while still adding
the column/index on existing prod DBs. Adding a NOT NULL column with a
constant default is metadata-only on Postgres 11+ (no table rewrite).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "trialkind01"
down_revision: Union[str, Sequence[str], None] = "dropanalyzers01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _recover_invalid_index(index_name: str) -> None:
    """An interrupted ``CREATE INDEX CONCURRENTLY`` leaves a same-name INVALID
    index behind (``pg_index.indisvalid = false``). ``IF NOT EXISTS`` sees that
    relation and skips the CREATE, so a retried migration would complete with
    an index that serves no queries. Drop the invalid leftover (concurrently,
    we are in the autocommit block) so the CREATE below rebuilds it."""
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
    # Bound the ALTER's brief ACCESS EXCLUSIVE lock: without a timeout it
    # queues behind any long-running query, and all traffic queues behind it.
    op.execute("SET lock_timeout = '8s'")
    op.add_column(
        "trials",
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            server_default="agent",
        ),
        if_not_exists=True,
    )
    # Index name matches the model's ``__table_args__`` declaration so the
    # create_all() index and this one are the same object.
    with op.get_context().autocommit_block():
        _recover_invalid_index("ix_trials_kind_non_agent")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_trials_kind_non_agent ON trials (kind) WHERE kind != 'agent'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_trials_kind_non_agent")
    op.execute("SET lock_timeout = '8s'")
    op.drop_column("trials", "kind", if_exists=True)
