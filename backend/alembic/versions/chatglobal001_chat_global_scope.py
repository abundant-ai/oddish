"""add api_keys.is_internal + chat_sessions.query_api_key_id

Revision ID: chatglobal001
Revises: q3r4s5t6u7v8
Create Date: 2026-06-15 00:00:00.000000

Idempotent: uses ADD COLUMN IF NOT EXISTS / DROP COLUMN IF EXISTS so this
migration is safe to re-run on a DB that already has the columns.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "chatglobal001"
down_revision: Union[str, Sequence[str], None] = "q3r4s5t6u7v8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
        "is_internal BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS "
        "query_api_key_id VARCHAR(64)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS is_internal")
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS query_api_key_id")
