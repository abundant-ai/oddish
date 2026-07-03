"""merge point + index reconciliation only

Revision ID: cost_settle_001
Revises: merge_billed_user_main_001, api_key_creator_role_001
Create Date: 2026-07-02 00:00:00.000000

* Reconciles ``idx_trials_org_billed_user_finished`` on environments that ran
  an earlier ``billed_user_001`` revision which built it PARTIAL
  (``deleted_at IS NULL``): the include-deleted spend sums can't use a partial
  index, so drop and recreate non-partial. No-op when already non-partial.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "cost_settle_001"
# Merge point: origin/main's reparented api_key_creator_role_001 arrived with
# nothing downstream referencing it, leaving the oddish tree two-headed.
down_revision: Union[str, Sequence[str], None] = (
    "merge_billed_user_main_001",
    "api_key_creator_role_001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        stale = op.get_bind().scalar(
            sa.text(
                "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid"
                " WHERE c.relname = 'idx_trials_org_billed_user_finished'"
                " AND (NOT i.indisvalid OR i.indpred IS NOT NULL)"
            )
        )
        if stale:
            op.execute(
                "DROP INDEX CONCURRENTLY idx_trials_org_billed_user_finished"
            )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_trials_org_billed_user_finished
            ON trials (org_id, billed_user_id, finished_at)
            """
        )


def downgrade() -> None:
    pass
