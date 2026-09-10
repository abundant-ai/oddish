"""Record delivery progress without changing shipping snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "delivery_progress_001"
down_revision = "task_defects_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_progress",
        sa.Column(
            "delivery_id",
            sa.String(64),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sample_hour", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("delivery_progress")
