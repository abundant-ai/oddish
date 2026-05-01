"""drop task_versions.updated_at

Task versions are immutable -- once written, they never change. The
``updated_at`` column inherited from ``TimestampedMixin`` was never
written to in practice but its presence implied mutability. Dropping it
makes the intent obvious in the schema. Real write-once enforcement at
the DB layer (CHECK trigger / role revoke) lands separately.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-05-01 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
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
