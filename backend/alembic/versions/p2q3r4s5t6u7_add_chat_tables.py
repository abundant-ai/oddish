"""add chat_sessions, chat_session_events, chat_turns

Revision ID: p2q3r4s5t6u7
Revises: o1p2q3r4s5t6
Create Date: 2026-06-14 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "p2q3r4s5t6u7"
down_revision: Union[str, Sequence[str], None] = "o1p2q3r4s5t6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id VARCHAR(64) PRIMARY KEY,
            org_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id VARCHAR(64),
            scope_kind VARCHAR(32) NOT NULL,
            scope_id VARCHAR(128) NOT NULL,
            sandbox_id VARCHAR(128),
            daytona_session_id VARCHAR(64) NOT NULL DEFAULT 'cc',
            claude_session_id VARCHAR(128),
            status VARCHAR(32) NOT NULL,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_activity TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_sessions_org_id ON chat_sessions (org_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_status_last_activity "
        "ON chat_sessions (status, last_activity)"
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
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_turns_session_seq ON chat_turns (session_id, seq)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_session_events")
    op.execute("DROP TABLE IF EXISTS chat_turns")
    op.execute("DROP TABLE IF EXISTS chat_sessions")
