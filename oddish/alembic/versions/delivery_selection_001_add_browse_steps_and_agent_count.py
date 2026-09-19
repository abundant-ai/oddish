"""Add step percentiles and agent count to task-browser summaries.

Revision ID: delivery_selection_001
Revises: delivery_history_001

The task browser selects tasks for a customer delivery, and two of the
selection criteria are not answerable from the existing summary row: how
long the task's trajectories are (to pick long-horizon tasks) and how many
distinct harnesses have run it (the delivery board's rollout check). Both
are now stored on ``task_version_browse_summaries`` so filtering and
sorting on them is an indexed row read, not an aggregate over trials.

Backfill is one statement over the same trial population
``oddish.core.task_browse_metrics.browse_trial_scope`` defines. Versions
with no in-scope trial keep the column defaults (0 / NULL). Deploys run
migrations before code, so a trial that settles between this backfill and
code cutover leaves its version stale only until its next refresh.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "delivery_selection_001"
down_revision: Union[str, Sequence[str], None] = "delivery_history_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in lockstep with oddish.core.task_browse_metrics.browse_trial_scope.
_SCOPE = """
    deleted_at IS NULL
    AND superseded_by_trial_id IS NULL
    AND is_probe IS NOT TRUE
    AND kind = 'agent'
    AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')
"""

_TABLE = "task_version_browse_summaries"


def _column_exists(table: str, name: str) -> bool:
    return name in {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)
    }


def upgrade() -> None:
    if not _column_exists(_TABLE, "steps_present"):
        op.add_column(
            _TABLE,
            sa.Column(
                "steps_present", sa.Integer(), nullable=False, server_default="0"
            ),
        )
    for name in ("steps_p25", "steps_p50", "steps_p75"):
        if not _column_exists(_TABLE, name):
            op.add_column(_TABLE, sa.Column(name, sa.Integer(), nullable=True))
    if not _column_exists(_TABLE, "agent_count"):
        op.add_column(
            _TABLE,
            sa.Column("agent_count", sa.Integer(), nullable=False, server_default="0"),
        )
    op.execute(
        f"""
        WITH aggregates AS (
            SELECT task_version_id,
                   COUNT(total_steps) AS steps_present,
                   CAST(percentile_disc(0.25) WITHIN GROUP (ORDER BY total_steps)
                        AS integer) AS steps_p25,
                   CAST(percentile_disc(0.5) WITHIN GROUP (ORDER BY total_steps)
                        AS integer) AS steps_p50,
                   CAST(percentile_disc(0.75) WITHIN GROUP (ORDER BY total_steps)
                        AS integer) AS steps_p75,
                   COUNT(DISTINCT agent) AS agent_count
            FROM trials
            WHERE task_version_id IS NOT NULL AND {_SCOPE}
            GROUP BY task_version_id
        )
        UPDATE {_TABLE} summary
        SET steps_present = aggregates.steps_present,
            steps_p25 = aggregates.steps_p25,
            steps_p50 = aggregates.steps_p50,
            steps_p75 = aggregates.steps_p75,
            agent_count = aggregates.agent_count
        FROM aggregates
        WHERE summary.task_version_id = aggregates.task_version_id
        """
    )


def downgrade() -> None:
    for name in ("agent_count", "steps_p75", "steps_p50", "steps_p25", "steps_present"):
        op.drop_column(_TABLE, name)
