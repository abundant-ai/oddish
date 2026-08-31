"""widen bound analysis trial ids to match trials.id

Revision ID: boundanalysis160
Revises: boundanalysis001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "boundanalysis160"
down_revision: str | Sequence[str] | None = "boundanalysis001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "api_keys",
        "bound_analysis_trial_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=160),
        existing_nullable=True,
    )


def downgrade() -> None:
    # PostgreSQL refuses this change while any stored ID exceeds 64 characters;
    # do not truncate an authorization boundary to force the downgrade through.
    op.alter_column(
        "api_keys",
        "bound_analysis_trial_id",
        existing_type=sa.String(length=160),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
