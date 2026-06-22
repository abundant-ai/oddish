"""add job-scoped credential token columns to worker_jobs

Adds the persisted side of the job-scoped credential bundle (design spec §6.6):
``job_token_hash`` (SHA-256 of the issued token -- the raw token is never
stored), ``job_token_expires_at``, and ``job_token_revoked_at``. Additive +
idempotent (IF NOT EXISTS); the feature is gated by
``settings.job_scoped_tokens_enabled`` (default off), so these stay NULL until
enabled.

Revision ID: wjtoken01_worker_jobs_job_token
Revises: wjstage01_worker_jobs_stage_timestamps
Create Date: 2026-06-23 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "wjtoken01_worker_jobs_job_token"
down_revision: Union[str, Sequence[str], None] = (
    "wjstage01_worker_jobs_stage_timestamps"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "worker_jobs",
        sa.Column("job_token_hash", sa.Text(), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "worker_jobs",
        sa.Column("job_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "worker_jobs",
        sa.Column("job_token_revoked_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("worker_jobs", "job_token_revoked_at", if_exists=True)
    op.drop_column("worker_jobs", "job_token_expires_at", if_exists=True)
    op.drop_column("worker_jobs", "job_token_hash", if_exists=True)
