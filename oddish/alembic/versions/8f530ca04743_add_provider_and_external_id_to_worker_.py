"""add provider and external_id to worker_jobs

Revision ID: 8f530ca04743
Revises: k2l3m4n5o6p7
Create Date: 2026-06-01 18:01:17.381946

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f530ca04743'
down_revision: Union[str, Sequence[str], None] = 'k2l3m4n5o6p7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "worker_jobs",
        sa.Column("provider", sa.Text(), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "worker_jobs",
        sa.Column("external_id", sa.Text(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("worker_jobs", "external_id", if_exists=True)
    op.drop_column("worker_jobs", "provider", if_exists=True)
