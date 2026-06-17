"""add is_probe column to trials

Revision ID: trial_is_probe_001
Revises: merge_tagfix_probe_heads
Create Date: 2026-06-08 12:00:00.000000

Adds a first-class, indexed ``is_probe`` boolean to ``trials`` and backfills it
from the existing JSONB marker (``harbor_config->>'mode' = 'probe'``). Lives in
the oddish migration tree (``alembic_version_oddish``).

Idempotent: ``000_initial_schema`` runs ``Base.metadata.create_all()``, so on a
fresh DB the column + ``ix_trials_is_probe`` index already exist by the time this
runs. The ``if_not_exists`` guards make the upgrade a no-op there while still
adding the column/index on existing prod DBs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "trial_is_probe_001"
down_revision: Union[str, Sequence[str], None] = "merge_tagfix_probe_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trials",
        sa.Column(
            "is_probe",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        if_not_exists=True,
    )
    # Backfill historical probe rows from the JSONB marker. No-op on a fresh DB.
    op.execute(
        "UPDATE trials SET is_probe = true " "WHERE harbor_config->>'mode' = 'probe'"
    )
    # Index name matches SQLAlchemy's default for ``index=True`` (ix_<table>_<col>)
    # so the model's create_all() index and this one are the same object.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_trials_is_probe ON trials (is_probe)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_trials_is_probe")
    op.drop_column("trials", "is_probe", if_exists=True)
