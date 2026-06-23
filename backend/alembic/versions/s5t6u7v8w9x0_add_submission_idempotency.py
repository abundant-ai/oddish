"""add submission_idempotency

Records an Idempotency-Key per (org, route) so a retried side-effecting
submission (POST /tasks/sweep) replays its stored response instead of creating
duplicate work. ``status`` is plain text + CHECK rather than a Postgres enum so
this migration downgrades cleanly.

Revision ID: s5t6u7v8w9x0
Revises: r4s5t6u7v8w9
Create Date: 2026-06-19 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "s5t6u7v8w9x0"
down_revision: Union[str, Sequence[str], None] = "r4s5t6u7v8w9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS submission_idempotency (
            id VARCHAR(64) PRIMARY KEY,
            org_id VARCHAR(64) NOT NULL,
            route VARCHAR(64) NOT NULL,
            key_hash VARCHAR(64) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'in_progress',
            response_json JSONB,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_submission_idempotency_status
                CHECK (status IN ('in_progress', 'completed'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_submission_idempotency_org_route_key "
        "ON submission_idempotency (org_id, route, key_hash)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_submission_idempotency_expires_at "
        "ON submission_idempotency (expires_at)"
    )


def downgrade() -> None:
    # Dropping the table also drops its indexes and CHECK constraint.
    op.execute("DROP TABLE IF EXISTS submission_idempotency")
