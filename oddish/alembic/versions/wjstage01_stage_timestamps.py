"""add per-stage timing + admission reason to worker_jobs

Adds the observability columns for the pre-harbor preamble (design spec §12):
``spawned_at`` and ``sandbox_creating_at`` (new stage markers; ``claimed_at`` /
``started_at`` already exist) plus ``admission_reason`` (the why-waiting field).
Additive + idempotent (IF NOT EXISTS), so re-applying on an inherited schema is
a no-op.

Revision ID: wjstage01_stage_timestamps
Revises: r8a9drop_probe_ratio_001
Create Date: 2026-06-22 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "wjstage01_stage_timestamps"
down_revision: Union[str, Sequence[str], None] = "r8a9drop_probe_ratio_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "worker_jobs",
        sa.Column("spawned_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "worker_jobs",
        sa.Column("sandbox_creating_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "worker_jobs",
        sa.Column("admission_reason", sa.Text(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("worker_jobs", "admission_reason", if_exists=True)
    op.drop_column("worker_jobs", "sandbox_creating_at", if_exists=True)
    op.drop_column("worker_jobs", "spawned_at", if_exists=True)
