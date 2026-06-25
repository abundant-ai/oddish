"""add operator_prompt/result_focus/evaluation_metric to skills

Revision ID: skills_directive_001
Revises: apk01dropfk
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "skills_directive_001"
down_revision: Union[str, Sequence[str], None] = "apk01dropfk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS operator_prompt TEXT")
    op.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS result_focus TEXT")
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS evaluation_metric VARCHAR(32)"
    )


def downgrade() -> None:
    op.drop_column("skills", "evaluation_metric")
    op.drop_column("skills", "result_focus")
    op.drop_column("skills", "operator_prompt")
