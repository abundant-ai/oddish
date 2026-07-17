"""add idx_trials_live_task_version on trials

Revision ID: 59f44b40b582
Revises: analyzers_007
Create Date: 2026-07-17 19:44:22.343459

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '59f44b40b582'
down_revision: Union[str, Sequence[str], None] = 'analyzers_007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Task-browser hot-path index: the correlated ``EXISTS`` trial filters and the
    ``GROUP BY (task_id, task_version_id)`` aggregates all scope trials to the
    task's current version, non-superseded, non-probe. Plain transactional
    ``CREATE INDEX`` so it applies reliably through the migration runner /
    preview (the pooled, transactional path can't run ``CONCURRENTLY``). On
    prod, pre-create it ``CONCURRENTLY`` out-of-band to avoid locking the hot
    ``trials`` table; ``if_not_exists`` then makes this a no-op there.
    """
    _live = "superseded_by_trial_id IS NULL AND is_probe IS NOT TRUE"
    _not_deleted = "deleted_at IS NULL"

    # P0 — correlated EXISTS + aggregate scope.
    op.create_index(
        "idx_trials_live_task_version",
        "trials",
        ["task_id", "task_version_id"],
        postgresql_where=sa.text(_live),
        if_not_exists=True,
    )
    # Covering variant for org-wide aggregate / compare / top-performer scans.
    op.create_index(
        "idx_trials_live_org_task_version_cover",
        "trials",
        ["org_id", "task_id", "task_version_id"],
        postgresql_where=sa.text(_live),
        postgresql_include=[
            "reward", "agent", "model", "status", "error_message",
            "started_at", "finished_at", "input_tokens", "output_tokens",
            "cache_tokens", "total_steps",
        ],
        if_not_exists=True,
    )
    # Task lead filters.
    op.create_index(
        "idx_tasks_org_status_live", "tasks", ["org_id", "status"],
        postgresql_where=sa.text(_not_deleted), if_not_exists=True,
    )
    op.create_index(
        "idx_tasks_org_priority_live", "tasks", ["org_id", "priority"],
        postgresql_where=sa.text(_not_deleted), if_not_exists=True,
    )
    op.create_index(
        "idx_tasks_org_verdict_status_live", "tasks", ["org_id", "verdict_status"],
        postgresql_where=sa.text(_not_deleted), if_not_exists=True,
    )
    # Tag filters + free-text tag matching.
    op.create_index(
        "idx_tasks_effective_tag_ids_gin", "tasks", ["effective_tag_ids"],
        postgresql_using="gin", if_not_exists=True,
    )
    # Experiments membership filter (task_id-leading).
    op.create_index(
        "idx_task_experiments_task_experiment_live", "task_experiments",
        ["task_id", "experiment_id"],
        postgresql_where=sa.text(_not_deleted), if_not_exists=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "idx_task_experiments_task_experiment_live",
        table_name="task_experiments", if_exists=True,
    )
    op.drop_index("idx_tasks_effective_tag_ids_gin", table_name="tasks", if_exists=True)
    op.drop_index("idx_tasks_org_verdict_status_live", table_name="tasks", if_exists=True)
    op.drop_index("idx_tasks_org_priority_live", table_name="tasks", if_exists=True)
    op.drop_index("idx_tasks_org_status_live", table_name="tasks", if_exists=True)
    op.drop_index(
        "idx_trials_live_org_task_version_cover", table_name="trials", if_exists=True,
    )
    op.drop_index("idx_trials_live_task_version", table_name="trials", if_exists=True)
