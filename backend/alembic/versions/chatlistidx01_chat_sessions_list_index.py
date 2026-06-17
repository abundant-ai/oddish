"""composite index for the chat-session list query

Revision ID: chatlistidx01
Revises: chatglobal001
Create Date: 2026-06-16 00:00:00.000000

The chat history list (`list_sessions`) filters by (org_id, scope_kind,
scope_id) and orders by last_activity desc. The only prior index was on
org_id alone, so for an org with many sessions the query degraded to an
org-wide scan + in-memory sort — observed at 19–43s under prod DB load.
This composite index serves both the filter and the ordering directly.

Idempotent: CREATE INDEX IF NOT EXISTS so it is safe to re-run.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "chatlistidx01"
down_revision: Union[str, Sequence[str], None] = "chatglobal001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_org_scope_activity "
        "ON chat_sessions (org_id, scope_kind, scope_id, last_activity DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_sessions_org_scope_activity")
