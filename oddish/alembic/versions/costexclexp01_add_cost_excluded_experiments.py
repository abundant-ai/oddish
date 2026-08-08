"""add cost_excluded_experiments

Revision ID: costexclexp01
Revises: prod_schema_repair_001
Create Date: 2026-08-08 00:00:00.000000

Admin-managed list of experiments whose trials' spend is excluded from cost
accounting -- the experiment-level sibling of ``cost_excluded_llm_keys``.
Exclusion correlates on ``trials.experiment_id`` (already indexed on trials);
``experiment_name`` is a display snapshot from registration time. ``deleted_at``
is the soft-delete tombstone; the partial UNIQUE keeps one live row per
experiment so a removed experiment can be re-added.

"""

from typing import Sequence, Union

from alembic import op

revision: str = "costexclexp01"
down_revision: Union[str, Sequence[str], None] = "prod_schema_repair_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_excluded_experiments (
            id VARCHAR(64) PRIMARY KEY,
            experiment_id VARCHAR(64) NOT NULL,
            experiment_name VARCHAR(255) NOT NULL DEFAULT '',
            label VARCHAR(255) NOT NULL DEFAULT '',
            created_by_user_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_excluded_experiments_exp_live
        ON cost_excluded_experiments (experiment_id) WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cost_excluded_experiments")
