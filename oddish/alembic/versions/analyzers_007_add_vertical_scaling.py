"""add analyzers.vertical_scaling_content

The task-investment sibling of headroom_analysis: recurring domain gotchas plus
briefs for tasks that would exercise them.

Nullable and un-indexed: a plain ADD COLUMN takes no FK lock, so it cannot deadlock.
``000_initial_schema`` runs ``create_all()``, so on a fresh DB the column already
exists before this migration runs -- hence ``if_not_exists=True``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "analyzers_007"
down_revision: Union[str, Sequence[str], None] = "analyzers_006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analyzers",
        sa.Column("vertical_scaling_content", sa.Text(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("analyzers", "vertical_scaling_content")
