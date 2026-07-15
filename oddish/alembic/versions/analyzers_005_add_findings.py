"""add analyzers.findings

Persists the per-trial findings the map phase already produces and ``_store``
previously discarded. Nullable and un-indexed: a plain ADD COLUMN takes no FK
lock, so it cannot deadlock the way a join-table migration can.

``000_initial_schema`` runs ``create_all()``, so on a fresh DB this column
already exists before this migration runs -- hence ``if_not_exists=True``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "analyzers_005"
down_revision: Union[str, Sequence[str], None] = "analyzers_004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analyzers",
        sa.Column("findings", JSONB, nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("analyzers", "findings")
