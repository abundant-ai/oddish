"""add_oddish_runtime_config

Tiny key-value table used for environment-local runtime config that
doesn't belong in env vars. Initial use: ``preview_epoch_at`` —
written by the cancel_cloned_preview_work script after it cancels
cloned production rows, read by the reaper so it only respawns rows
created after the preview branch started.

Revision ID: b4c5d6e7f8a9
Revises: k2l3m4n5o6p7
Create Date: 2026-05-21 22:50:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oddish_runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS oddish_runtime_config")
