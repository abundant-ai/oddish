"""add trials.kind

Revision ID: trial_kind_001
Revises: task_browse_summary_002
Create Date: 2026-08-07 00:00:00.000000

'agent' is a normal evaluation run. 'qa', 'audit', 'analyzer_map', and
'analyzer_reduce' are the platform's own analysis agents, which now run
through the trial pipeline. The partial index serves the exclusion filters
on user-facing surfaces: almost every trial is 'agent', so a full index
would be nearly all one value.

Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "trial_kind_001"
down_revision: Union[str, Sequence[str], None] = "task_browse_summary_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS "
        "kind VARCHAR(32) NOT NULL DEFAULT 'agent'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trials_kind_non_agent "
        "ON trials (kind) WHERE kind != 'agent'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trials_kind_non_agent")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS kind")
