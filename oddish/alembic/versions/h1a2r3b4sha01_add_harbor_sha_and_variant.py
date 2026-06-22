"""add trials.harbor_sha and worker_jobs.harbor_variant_id

Revision ID: h1a2r3b4sha01
Revises: r8a9drop_probe_ratio_001
Create Date: 2026-06-22

Core-table columns for the configurable-Harbor-source feature. trials and
worker_jobs are core tables, so this DDL lives in the oddish alembic stack only.

Idempotent: ``000_initial_schema`` runs ``Base.metadata.create_all()``, so on a
fresh DB the columns + ``ix_trials_harbor_sha`` index already exist by the time
this runs. The ``if_not_exists`` / ``CONCURRENTLY IF NOT EXISTS`` guards make the
upgrade a no-op there while still adding the column/index on existing prod DBs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h1a2r3b4sha01"
down_revision: Union[str, Sequence[str], None] = "r8a9drop_probe_ratio_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trials",
        sa.Column("harbor_sha", sa.String(length=64), nullable=True),
        if_not_exists=True,
    )
    # Index name matches SQLAlchemy's default for ``index=True`` (ix_<table>_<col>)
    # so the model's create_all() index and this one are the same object.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_trials_harbor_sha ON trials (harbor_sha)"
        )
    op.add_column(
        "worker_jobs",
        sa.Column(
            "harbor_variant_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("worker_jobs", "harbor_variant_id", if_exists=True)
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_trials_harbor_sha")
    op.drop_column("trials", "harbor_sha", if_exists=True)
