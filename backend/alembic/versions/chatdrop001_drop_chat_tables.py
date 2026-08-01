"""drop chat_sessions, chat_session_events, chat_turns

Revision ID: chatdrop001
Revises: slack_user_daily_overage_002
Create Date: 2026-08-01 00:00:00.000000

The cc_chat feature has been removed; drop its tables. Children first so the
FKs to chat_sessions never dangle. Idempotent: DROP TABLE IF EXISTS is a no-op
on a DB that never had the chat tables.

``api_keys.is_internal`` (added alongside the chat tables in chatglobal001)
stays: it is generic api_keys schema, not chat-specific.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "chatdrop001"
down_revision: Union[str, Sequence[str], None] = "slack_user_daily_overage_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_session_events")
    op.execute("DROP TABLE IF EXISTS chat_turns")
    op.execute("DROP TABLE IF EXISTS chat_sessions")


def downgrade() -> None:
    # Recreate the final shape of the chat tables (p2q3r4s5t6u7 + q3r4s5t6u7v8
    # title + chatglobal001 query_api_key_id + chatlistidx01 list index) so a
    # downgrade lands on the schema the preceding revisions expect.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id VARCHAR(64) PRIMARY KEY,
            org_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id VARCHAR(64),
            scope_kind VARCHAR(32) NOT NULL,
            scope_id VARCHAR(128) NOT NULL,
            sandbox_id VARCHAR(128),
            query_api_key_id VARCHAR(64),
            daytona_session_id VARCHAR(64) NOT NULL DEFAULT 'cc',
            claude_session_id VARCHAR(128),
            status VARCHAR(32) NOT NULL,
            error TEXT,
            title TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_activity TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_org_id ON chat_sessions (org_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_status_last_activity "
        "ON chat_sessions (status, last_activity)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_org_scope_activity "
        "ON chat_sessions (org_id, scope_kind, scope_id, last_activity DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_session_events (
            id VARCHAR(64) PRIMARY KEY,
            session_id VARCHAR(64) NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            event JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_chat_session_events_session_seq UNIQUE (session_id, seq)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_session_events_session_seq "
        "ON chat_session_events (session_id, seq)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_turns (
            id VARCHAR(64) PRIMARY KEY,
            session_id VARCHAR(64) NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            user_message TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ,
            error TEXT
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_turns_one_running "
        "ON chat_turns (session_id) WHERE status = 'running'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_turns_session_seq "
        "ON chat_turns (session_id, seq)"
    )
