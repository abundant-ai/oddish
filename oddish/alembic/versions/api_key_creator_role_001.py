"""add api key creator role

Revision ID: api_key_creator_role_001
Revises: exp_trials_join_001
Create Date: 2026-07-02 18:45:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "api_key_creator_role_001"
down_revision: Union[str, Sequence[str], None] = "exp_trials_join_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded like apk01dropfk: api_keys is a backend-stack table (created by
    # the backend chain / create_all), so the oddish chain must not hard-require
    # it -- OSS DBs migrate without it.
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
