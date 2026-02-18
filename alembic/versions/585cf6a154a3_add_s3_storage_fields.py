"""add_s3_storage_fields

Revision ID: 585cf6a154a3
Revises: 704895440a03
Create Date: 2026-01-13 16:10:56.761991

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "585cf6a154a3"
down_revision: Union[str, Sequence[str], None] = "704895440a03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add S3 storage fields (idempotent for local/dev resets)
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_s3_key TEXT")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS trial_s3_key TEXT")


def downgrade() -> None:
    """Downgrade schema."""
    # Remove S3 storage fields
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS trial_s3_key")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS task_s3_key")
