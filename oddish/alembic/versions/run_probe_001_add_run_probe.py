"""add run_probe column to tasks

Revision ID: run_probe_001
Revises: mine_filter_idx_001
Create Date: 2026-06-10 12:00:00.000000

Adds a ``run_probe`` boolean to ``tasks`` so auto-probing is opt-in (off by
default) instead of running for every sweep. Mirrors the existing
``run_analysis`` opt-in flag. Lives in the oddish migration tree
(``alembic_version_oddish``).

Idempotent: ``000_initial_schema`` runs ``Base.metadata.create_all()``, so on a
fresh DB the column already exists by the time this runs. The ``if_not_exists``
guard makes the upgrade a no-op there while still adding the column on existing
prod DBs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "run_probe_001"
down_revision: Union[str, Sequence[str], None] = "mine_filter_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "run_probe",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("tasks", "run_probe", if_exists=True)
