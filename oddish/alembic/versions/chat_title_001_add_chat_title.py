"""add title column to chat_sessions

Revision ID: chat_title_001
Revises: probe_runs_idx_001
Create Date: 2026-06-15 00:00:00.000000

Adds a nullable ``title`` to ``chat_sessions`` (set from the first user message).
Lives in the oddish migration tree (``alembic_version_oddish``). Idempotent:
``000_initial_schema`` runs ``create_all()``, so on a fresh DB the column already
exists; ``if_not_exists`` makes the upgrade a no-op there while adding it on
existing DBs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "chat_title_001"
down_revision: Union[str, Sequence[str], None] = "probe_runs_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("title", sa.Text(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "title", if_exists=True)
