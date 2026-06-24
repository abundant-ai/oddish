"""fold harbor_variant_id into idx_worker_jobs_claim

Revision ID: hv2claimidx01
Revises: h1a2r3b4sha01
Create Date: 2026-06-22

The worker-job claim predicate now filters on (queue_key, harbor_variant_id),
so the hot partial claim index must lead with both columns or the claim path
loses index coverage and table-scans under load. worker_jobs is a core table,
so this DDL lives in the oddish alembic stack only.

Idempotent: ``000_initial_schema`` runs ``Base.metadata.create_all()``, so on a
fresh DB the index already exists with the new column set. The drop-then-create
(both ``CONCURRENTLY``, ``IF EXISTS`` / ``IF NOT EXISTS``) rebuilds it in place
on existing prod DBs and is a cheap no-op shape on a fresh one.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "hv2claimidx01"
down_revision: Union[str, Sequence[str], None] = "h1a2r3b4sha01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_INDEX = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_worker_jobs_claim "
    "ON worker_jobs (queue_key, priority, available_after, created_at) "
    "WHERE status IN ('QUEUED', 'RETRYING')"
)
_NEW_INDEX = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_worker_jobs_claim "
    "ON worker_jobs (queue_key, harbor_variant_id, priority, available_after, "
    "created_at) WHERE status IN ('QUEUED', 'RETRYING')"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_worker_jobs_claim")
        op.execute(_NEW_INDEX)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_worker_jobs_claim")
        op.execute(_OLD_INDEX)
