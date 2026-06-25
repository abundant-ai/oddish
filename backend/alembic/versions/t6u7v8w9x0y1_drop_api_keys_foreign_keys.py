"""drop api_keys foreign keys"""

from typing import Sequence, Union

from alembic import op

revision: str = "t6u7v8w9x0y1"
down_revision: Union[str, Sequence[str], None] = "s5t6u7v8w9x0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINTS = (
    "api_keys_org_id_fkey",
    "api_keys_created_by_user_id_fkey",
    "fk_api_keys_org_id",
    "fk_api_keys_created_by_user_id",
)


def upgrade() -> None:
    for constraint in _CONSTRAINTS:
        op.execute(
            f"ALTER TABLE IF EXISTS api_keys DROP CONSTRAINT IF EXISTS {constraint}"
        )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.api_keys') IS NOT NULL
               AND to_regclass('public.organizations') IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM pg_constraint WHERE conname = 'fk_api_keys_org_id'
               ) THEN
                ALTER TABLE api_keys
                ADD CONSTRAINT fk_api_keys_org_id
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.api_keys') IS NOT NULL
               AND to_regclass('public.users') IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM pg_constraint
                   WHERE conname = 'fk_api_keys_created_by_user_id'
               ) THEN
                ALTER TABLE api_keys
                ADD CONSTRAINT fk_api_keys_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )
