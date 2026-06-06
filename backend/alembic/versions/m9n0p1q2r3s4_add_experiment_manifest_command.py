"""add_experiment_manifest_command

Adds experiments.manifest and experiments.command columns to capture the
experiment spec (raw sweep config + verbatim CLI invocation) a run was
generated from, so it can be replicated/tracked. Both nullable. These are
internal-only: never exposed on public/share responses.

Revision ID: m9n0p1q2r3s4
Revises: l8m9n0p1q2r3
Create Date: 2026-06-06 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "m9n0p1q2r3s4"
down_revision: Union[str, Sequence[str], None] = "l8m9n0p1q2r3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS manifest TEXT")
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS command TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS command")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS manifest")
