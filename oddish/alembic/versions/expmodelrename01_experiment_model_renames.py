"""move model display names onto the experiment

Revision ID: expmodelrename01
Revises: trajsum_002
Create Date: 2026-08-19 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "expmodelrename01"
down_revision: Union[str, Sequence[str], None] = "trajsum_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS public_model_renames JSONB"
    )
    op.execute("DROP TABLE IF EXISTS model_display_names")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_display_names (
            id VARCHAR(64) PRIMARY KEY,
            model_name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255) NOT NULL,
            created_by_user_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_display_names_model_live
        ON model_display_names (model_name) WHERE deleted_at IS NULL
        """
    )
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS public_model_renames")
