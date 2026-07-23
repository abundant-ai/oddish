"""add pre-trial task columns + analyzer_blocks prompt-version columns"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "prompts_002"
down_revision: Union[str, Sequence[str], None] = "prompts_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_col(bind, table, col) -> bool:
    return col in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_col(bind, "tasks", "pre_trial"):
        op.add_column("tasks", sa.Column("pre_trial", sa.dialects.postgresql.JSONB, nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_status", sa.Enum(name="jobstatus", create_type=False), nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_error", sa.Text, nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_started_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_finished_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_col(bind, "analyzer_blocks", "prompt_key"):
        op.add_column("analyzer_blocks", sa.Column("prompt_key", sa.String(128), nullable=True))
        op.add_column("analyzer_blocks", sa.Column("prompt_version", sa.Integer, nullable=True))


def downgrade() -> None:
    for col in ("pre_trial_finished_at", "pre_trial_started_at", "pre_trial_error", "pre_trial_status", "pre_trial"):
        op.drop_column("tasks", col)
    op.drop_column("analyzer_blocks", "prompt_version")
    op.drop_column("analyzer_blocks", "prompt_key")
