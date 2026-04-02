"""drop_pgqueuer

Drop PGQueuer tables and the current_pgqueuer_job_id column from trials.
The trials table now serves as the queue directly.

Revision ID: s5t6u7v8w9x0
Revises: r4s5t6u7v8w9
Create Date: 2026-04-02 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "s5t6u7v8w9x0"
down_revision: Union[str, Sequence[str], None] = "r4s5t6u7v8w9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trials_current_pgqueuer_job_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS current_pgqueuer_job_id")
    op.execute("DROP TABLE IF EXISTS pgqueuer_log")
    op.execute("DROP TABLE IF EXISTS pgqueuer")
    op.execute("DROP TYPE IF EXISTS pgqueuer_status")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS current_pgqueuer_job_id INTEGER"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trials_current_pgqueuer_job_id "
        "ON trials (current_pgqueuer_job_id)"
    )
