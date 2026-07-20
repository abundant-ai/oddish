"""add analyzers.by_model

Per-model report insights: normalized model key -> narrative, relative
strengths/weaknesses, distinctive failures, plus the trial-derived denominators
those claims are made against.

NULL means "generated before per-model analysis" and drives the frontend's
legacy four-section render, so it must stay distinguishable from an empty
payload. There is no backfill; re-running a report is the only way to populate it.

Nullable and un-indexed: a plain ADD COLUMN takes no FK lock, so it cannot
deadlock. ``000_initial_schema`` runs ``create_all()``, so on a fresh DB the
column already exists before this migration runs -- hence ``if_not_exists=True``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "analyzers_008"
down_revision: Union[str, Sequence[str], None] = "legacy_imported_at_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analyzers",
        sa.Column("by_model", JSONB, nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("analyzers", "by_model")
