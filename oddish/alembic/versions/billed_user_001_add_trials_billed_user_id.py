"""add trials.billed_user_id + per-user daily spend index

Revision ID: billed_user_001
Revises: a1b2c3d4e5f6
Create Date: 2026-07-01 00:00:00.000000

Adds the per-user quota attribution column and the partial index the daily
spend SUM keys on.

* ``trials.billed_user_id`` — nullable ``VARCHAR(64)``, denormalized and
  UNCONSTRAINED (NOT a foreign key: ``users`` lives in the backend package, so a
  cross-package FK would break OSS installs). NULL means the trial's cost draws
  down nobody's quota (imported / combined rows).
* ``idx_trials_org_billed_user_finished`` — partial composite backing
  ``WHERE org_id = ? AND billed_user_id = ? AND finished_at >= ? AND
  deleted_at IS NULL``. Built ``CONCURRENTLY`` (outside a transaction via
  ``autocommit_block``) so it is safe against a busy table. No backfill and no
  data statements: pre-rollout rows stay NULL (harmless -- outside today's
  window), and missing quota rows enforce at the read-time default.

NOTE: ``down_revision`` targets the most recent trials-column migration
(``a1b2c3d4e5f6`` add_trial_cache_write_tokens). Confirm the single head at
merge time -- the repo's revision graph currently has multiple branches.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "billed_user_001"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS billed_user_id VARCHAR(64)"
    )
    # ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction; oddish
    # migrations run in one (see alembic/env.py), so drop to autocommit.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_trials_org_billed_user_finished
            ON trials (org_id, billed_user_id, finished_at)
            WHERE deleted_at IS NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_trials_org_billed_user_finished"
        )
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS billed_user_id")
