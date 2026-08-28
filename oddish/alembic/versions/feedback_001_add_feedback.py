"""add append-only QA feedback table

Agree/disagree votes are anchored to an experiment and trial. Reference
columns stay plain strings so this migration does not lock the hot tables.

Revision ID: feedback_001
Revises: expmodelrename02
Create Date: 2026-08-19 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "feedback_001"
down_revision: Union[str, Sequence[str], None] = "expmodelrename02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``000_initial_schema`` bootstraps a fresh database via
    # ``Base.metadata.create_all`` (every current model table), so on the
    # from-scratch path used by data-less preview branches this table already
    # exists when this revision runs. Skip to stay idempotent, matching the
    # ``CREATE TABLE IF NOT EXISTS`` convention the other migrations use.
    if sa.inspect(op.get_bind()).has_table("feedback"):
        return
    op.create_table(
        "feedback",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("trial_id", sa.String(160), nullable=False),
        sa.Column("target", sa.String(32), nullable=False),
        sa.Column("target_key", sa.String(160), nullable=False),
        sa.Column("vote", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target IN ('qa_verdict', 'qa_action_item')",
            name="ck_feedback_target",
        ),
        sa.CheckConstraint(
            "vote IN ('agree', 'disagree')",
            name="ck_feedback_vote",
        ),
    )


def downgrade() -> None:
    op.drop_table("feedback")
