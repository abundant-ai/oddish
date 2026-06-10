"""add experiments.owner_user_id

Revision ID: experiments_owner_001
Revises: documents_001
Create Date: 2026-06-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "experiments_owner_001"
down_revision: Union[str, Sequence[str], None] = "documents_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS owner_user_id VARCHAR(64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiments_org_owner_user_live "
        "ON experiments (org_id, owner_user_id) "
        "WHERE deleted_at IS NULL"
    )
    # Owner is stamped on new sweeps with GitHub-first precedence. Historical
    # experiments keep owner_user_id NULL and use the legacy primary-task
    # Mine filter until re-submitted or manually corrected.


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_experiments_org_owner_user_live")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS owner_user_id")
