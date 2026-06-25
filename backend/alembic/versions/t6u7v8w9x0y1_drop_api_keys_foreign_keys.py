"""drop api_keys foreign keys"""

from typing import Sequence, Union

from alembic import op

revision: str = "t6u7v8w9x0y1"
down_revision: Union[str, Sequence[str], None] = "s5t6u7v8w9x0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_org_id_fkey")
    op.execute(
        "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_created_by_user_id_fkey"
    )
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS fk_api_keys_org_id")
    op.execute(
        "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS fk_api_keys_created_by_user_id"
    )


def downgrade() -> None:
    pass
