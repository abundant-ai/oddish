"""add trial_events table

Revision ID: trial_events_001
Revises: org_quota_idx_001
Create Date: 2026-07-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "trial_events_001"
down_revision: Union[str, Sequence[str], None] = "org_quota_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``000_initial_schema`` bootstraps fresh databases via
    # ``Base.metadata.create_all``, so this table may already exist there.
    if sa.inspect(op.get_bind()).has_table("trial_events"):
        return
    op.create_table(
        "trial_events",
        sa.Column("trial_id", sa.String(160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["trial_id"], ["trials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("trial_id", "attempt", "seq"),
    )


def downgrade() -> None:
    op.drop_table("trial_events")
