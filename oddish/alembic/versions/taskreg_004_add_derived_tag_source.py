"""Add DERIVED to the tag_assignment_source native enum.

Revision ID: taskreg_004
Revises: taskreg_003

ALTER TYPE ... ADD VALUE cannot run inside a transaction block, hence the
autocommit block. There is no supported way to remove an enum value, so the
downgrade is intentionally a no-op.
"""

from __future__ import annotations

from alembic import op

revision = "taskreg_004"
down_revision = "taskreg_003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE tag_assignment_source ADD VALUE IF NOT EXISTS 'DERIVED'"
        )


def downgrade() -> None:
    pass
