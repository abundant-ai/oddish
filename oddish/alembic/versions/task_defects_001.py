"""Retain reported findings and version-specific delivery decisions.

No historical artifacts or committed delivery snapshots are rewritten.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "task_defects_001"
down_revision = "delivery_qa_work_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_versions",
        sa.Column("reported_findings", postgresql.JSONB(), nullable=True),
        if_not_exists=True,
    )
    op.execute("DROP INDEX IF EXISTS uq_delivery_manual_checks_task")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_manual_checks_task_version ON delivery_manual_checks (delivery_id, delivery_task_id, check_key, task_version_id) WHERE delivery_task_id IS NOT NULL"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Task finding and version-decision history must be retained; use a forward migration."
    )
