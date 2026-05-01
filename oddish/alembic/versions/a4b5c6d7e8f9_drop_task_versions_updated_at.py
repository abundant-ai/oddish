"""drop task_versions.updated_at

Task versions are immutable -- once written, they never change. The
``updated_at`` column inherited from ``TimestampedMixin`` was never
written to in practice but its presence implied mutability. Dropping it
makes the intent obvious in the schema. Real write-once enforcement at
the DB layer (CHECK trigger / role revoke) lands separately.

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-05-01 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "z3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE task_versions DROP COLUMN IF EXISTS updated_at")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE task_versions "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ "
        "NOT NULL DEFAULT now()"
    )
