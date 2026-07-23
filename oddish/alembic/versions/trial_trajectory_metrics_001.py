"""Persist searchable trajectory duration and tool-call metrics."""

from alembic import op


revision = "trial_trajectory_metrics_001"
down_revision = "prompts_merge_001"
branch_labels = depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS trajectory_duration_seconds DOUBLE PRECISION")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS total_tool_calls INTEGER")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS tool_counts JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS tool_counts")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS total_tool_calls")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS trajectory_duration_seconds")
