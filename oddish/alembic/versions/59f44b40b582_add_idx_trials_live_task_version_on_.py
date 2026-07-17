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
    op.create_index(
        "idx_trials_live_task_version",
        "trials",
        ["task_id", "task_version_id"],
        postgresql_where=sa.text(
            "superseded_by_trial_id IS NULL AND is_probe IS NOT TRUE"
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "idx_trials_live_task_version",
        table_name="trials",
        if_exists=True,
    )
