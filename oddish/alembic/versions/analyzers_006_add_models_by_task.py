"""add analyzers.models_by_task

Per-task roster of the models that RAN each task, including those that passed it.
The Task Construction lane's models-hit ratio needs it as a denominator, and it
cannot come from ``findings`` (which record only failures).

Nullable and un-indexed: a plain ADD COLUMN takes no FK lock, so it cannot
deadlock. ``000_initial_schema`` runs ``create_all()``, so on a fresh DB the
column already exists before this migration runs -- hence ``if_not_exists=True``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "analyzers_006"
down_revision: Union[str, Sequence[str], None] = "analyzers_005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analyzers",
        sa.Column("models_by_task", JSONB, nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("analyzers", "models_by_task")
