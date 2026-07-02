"""add trials.cost_settled_attempt + reconcile billed-user index

Revision ID: cost_settle_001
Revises: merge_billed_user_main_001, api_key_creator_role_001
Create Date: 2026-07-02 00:00:00.000000

* ``trials.cost_settled_attempt`` — last attempt whose outcome settled
  ``cost_usd``; gates at-least-once outcome redelivery so accumulation never
  double-bills an attempt. Backfill: FINISHED rows stamp ``attempts``;
  unfinished rows with a preserved prior-attempt cost (mid-retry) stamp
  ``attempts - 1`` so the LIVE attempt's own outcome still settles. Accepted
  one-time side effect: pre-existing FINISHED floored rows get a stamped
  marker, suppressing floor->late-real replacement for those historical rows
  only (safe direction -- never over-bills).
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
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS "
        "cost_settled_attempt INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        """
        UPDATE trials
        SET    cost_settled_attempt = CASE
                   WHEN finished_at IS NOT NULL THEN attempts
                   ELSE GREATEST(attempts - 1, 0)
               END
        WHERE  cost_settled_attempt = 0
          AND  (cost_usd IS NOT NULL OR finished_at IS NOT NULL)
        """
    )
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
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS cost_settled_attempt")
