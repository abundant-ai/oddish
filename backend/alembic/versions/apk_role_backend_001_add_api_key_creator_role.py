"""backend-chain mirror of oddish api_key_creator_role_001

Revision ID: apk_role_backend_001
Revises: merge_quota_main_001
Create Date: 2026-07-02 00:00:00.000000

The oddish-chain revision guards on api_keys existing (OSS DBs have no
backend stack), so a fresh install migrated in the documented order (oddish
first, then backend) would otherwise end at both heads WITHOUT
``api_keys.created_by_role`` -- a1b2c3d4e5f6 creates the table without it.
Idempotent in every order: existing DBs no-op via ADD COLUMN IF NOT EXISTS;
the backfill only touches rows still NULL.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "apk_role_backend_001"
down_revision: Union[str, Sequence[str], None] = "merge_quota_main_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.api_keys') IS NOT NULL THEN
                ALTER TABLE api_keys
                ADD COLUMN IF NOT EXISTS created_by_role VARCHAR(32);
                IF to_regclass('public.users') IS NOT NULL THEN
                    EXECUTE '
                        UPDATE api_keys
                        SET created_by_role = users.role::text
                        FROM users
                        WHERE api_keys.created_by_role IS NULL
                          AND api_keys.created_by_user_id = users.id
                    ';
                END IF;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE IF EXISTS api_keys DROP COLUMN IF EXISTS created_by_role"
    )
