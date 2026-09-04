"""Store task-version QA ownership and triage metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "delivery_qa_work_001"
down_revision = "qa_dispatch_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_versions", sa.Column("qa_work", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("task_versions", "qa_work")
