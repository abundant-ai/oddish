"""drop api_keys cross-stack foreign keys

Revision ID: t6u7v8w9x0y1
Revises: s5t6u7v8w9x0
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "t6u7v8w9x0y1"
down_revision: Union[str, Sequence[str], None] = "s5t6u7v8w9x0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS fk_api_keys_org_id")
    op.execute(
        "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS fk_api_keys_created_by_user_id"
    )
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_org_id_fkey")
    op.execute(
        "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_created_by_user_id_fkey"
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM api_keys
        WHERE org_id NOT IN (SELECT id FROM organizations)
        """
    )
    op.execute(
        """
        UPDATE api_keys
        SET created_by_user_id = NULL
        WHERE created_by_user_id IS NOT NULL
          AND created_by_user_id NOT IN (SELECT id FROM users)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_api_keys_org_id'
            ) THEN
                ALTER TABLE api_keys
                ADD CONSTRAINT fk_api_keys_org_id
                FOREIGN KEY (org_id) REFERENCES organizations (id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_api_keys_created_by_user_id'
            ) THEN
                ALTER TABLE api_keys
                ADD CONSTRAINT fk_api_keys_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )
