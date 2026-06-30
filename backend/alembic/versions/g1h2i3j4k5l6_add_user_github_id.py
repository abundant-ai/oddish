"""add users.github_id

Adds the immutable Clerk ``provider_user_id`` (TEXT) so owner resolution can
survive ``github_username`` renames/recycles. Org-scoped unique + index for the
lookup; NULLs are distinct in Postgres, so existing all-NULL rows never collide
(no global unique). Idempotent so a create_all-built preview replays cleanly.

Revision ID: g1h2i3j4k5l6
Revises: s5t6u7v8w9x0
Create Date: 2026-06-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, Sequence[str], None] = "s5t6u7v8w9x0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_id TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_org_github_id "
        "ON users (org_id, github_id)"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_users_org_github_id'
            ) THEN
                ALTER TABLE users
                ADD CONSTRAINT uq_users_org_github_id UNIQUE (org_id, github_id);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_org_github_id"
    )
    op.execute("DROP INDEX IF EXISTS idx_users_org_github_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS github_id")
