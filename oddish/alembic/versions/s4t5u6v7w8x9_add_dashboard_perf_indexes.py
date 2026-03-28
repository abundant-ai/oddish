"""add_dashboard_perf_indexes

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-03-28 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "s4t5u6v7w8x9"
down_revision: Union[str, Sequence[str], None] = "r3s4t5u6v7w8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add indexes to speed up dashboard queries."""
    # Supports DISTINCT ON (experiment_id) ORDER BY created_at DESC
    # used by the experiments-table "latest task" lookup.
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_experiment_created_at "
        "ON tasks (experiment_id, created_at DESC)"
    )
    # Covers the dashboard usage GROUP BY (model, provider) with org_id filter.
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trials_org_model_provider "
        "ON trials (org_id, model, provider)"
    )


def downgrade() -> None:
    """Remove dashboard performance indexes."""
    op.execute("DROP INDEX IF EXISTS idx_tasks_experiment_created_at")
    op.execute("DROP INDEX IF EXISTS idx_trials_org_model_provider")
