"""add durable trajectory-summary refresh pointer

Revision ID: trajsum_002
Revises: costexcl02
Create Date: 2026-08-20 00:00:00.000000

``trials.trajectory_summary_refresh_trial_id`` identifies the summarize trial
responsible for the target agent trial's next published summary. Existing rows
stay NULL; no historical scan or backfill runs during deployment. The partial
index contains only targets with an unfinished refresh and supports the bounded
cleanup reconciliation query.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.db.migration_locks import run_with_lock_retry

revision: str = "trajsum_002"
down_revision: Union[str, Sequence[str], None] = "costexcl02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _recover_invalid_index(index_name: str) -> None:
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
    if result.first() is not None:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


def upgrade() -> None:
    run_with_lock_retry(
        lambda: op.add_column(
            "trials",
            sa.Column(
                "trajectory_summary_refresh_trial_id",
                sa.String(160),
                nullable=True,
            ),
            if_not_exists=True,
        ),
        table_name="trials",
    )

    def _create_index() -> None:
        index_name = "ix_trials_trajectory_summary_refresh_trial_id"
        _recover_invalid_index(index_name)
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            f"{index_name} ON trials (trajectory_summary_refresh_trial_id) "
            "WHERE trajectory_summary_refresh_trial_id IS NOT NULL"
        )

    run_with_lock_retry(_create_index, table_name="trials")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_trials_trajectory_summary_refresh_trial_id"
        )
    op.execute("SET lock_timeout = '8s'")
    op.drop_column("trials", "trajectory_summary_refresh_trial_id", if_exists=True)
