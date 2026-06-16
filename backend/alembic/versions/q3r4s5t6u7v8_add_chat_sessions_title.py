"""add title column to chat_sessions

Revision ID: q3r4s5t6u7v8
Revises: p2q3r4s5t6u7
Create Date: 2026-06-15 00:00:00.000000

Adds a nullable ``title`` to ``chat_sessions`` (set from the first user message).
Backend tree (``alembic_version``), where the chat tables live. Idempotent:
the chat tables are created with ``IF NOT EXISTS`` raw SQL that the ORM model
mirrors, so this uses ``ADD COLUMN IF NOT EXISTS`` to be a no-op on a DB that
already has the column.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "q3r4s5t6u7v8"
down_revision: Union[str, Sequence[str], None] = "p2q3r4s5t6u7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS title TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS title")
