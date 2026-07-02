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
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS created_by_role VARCHAR(32)")
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.users') IS NOT NULL THEN
                EXECUTE '
                    UPDATE api_keys
                    SET created_by_role = users.role::text
                    FROM users
                    WHERE api_keys.created_by_role IS NULL
                      AND api_keys.created_by_user_id = users.id
                ';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS created_by_role")
