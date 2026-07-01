"""merge model_concurrency_advisory and worker_jobs_job_token heads

Empty merge migration unifying the two alembic heads created when #411
(``mca01_model_concurrency_advisory``) and #412 (``wjtoken01_worker_jobs_job_token``,
the stage-timestamps + job-token chain) both branched off ``exp_owner_link_001``.
#411 merged first (single head, applied fine); #412's merge added the second head,
so ``alembic upgrade head`` began failing with "Multiple head revisions". This
merge restores a single head with no schema change, so the migration pipeline
runs and both branches' migrations (incl. wjstage01's worker_jobs stage columns)
apply.

Revision ID: merge_mca01_wjtoken_heads
Revises: mca01_model_concurrency_advisory, wjtoken01_worker_jobs_job_token
Create Date: 2026-07-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "merge_mca01_wjtoken_heads"
down_revision: Union[str, Sequence[str], None] = (
    "mca01_model_concurrency_advisory",
    "wjtoken01_worker_jobs_job_token",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge point -- no schema change."""
    pass


def downgrade() -> None:
    """Merge point -- no schema change."""
    pass
