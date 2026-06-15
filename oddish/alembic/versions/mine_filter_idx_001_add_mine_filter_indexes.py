"""add mine filter support indexes

Revision ID: mine_filter_idx_001
Revises: experiments_owner_001
Create Date: 2026-06-11 00:00:00.000000

Adds the partial indexes the dashboard Mine filter needs to hit the fast
indexed path:

* ``idx_experiments_org_owner_activity_live`` — composite key + activity
  sort + tiebreaker so the Mine query can seek by ``(org_id,
  owner_user_id)`` and walk the index in ``last_activity_at DESC``,
  ``id ASC`` order without a separate sort.
* ``idx_tasks_org_created_by_live`` / ``idx_tasks_org_lower_user_live`` /
  ``idx_tasks_org_lower_github_tag_live`` — back the attribution
  discovery scans and the legacy Mine EXISTS fallback predicates so
  large orgs don't seq-scan ``tasks`` while the owner column is still
  converging via the sweep backfill.

All indexes are created ``CONCURRENTLY`` and ``IF NOT EXISTS`` so the
migration is safe to apply against a busy database. ``CONCURRENTLY``
requires running outside a transaction, which Alembic supports via the
``with op.get_context().autocommit_block():`` pattern below.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "mine_filter_idx_001"
down_revision: Union[str, Sequence[str], None] = "experiments_owner_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction.
    with op.get_context().autocommit_block():
        # Mine page query fast path: seek by ``(org_id, owner_user_id)``,
        # then walk the index in ``last_activity_at DESC NULLS LAST,
        # id ASC`` order so the dashboard sort doesn't need a separate
        # sort step. ``id ASC`` is the tiebreaker the ORM appends to
        # keep pagination stable.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_experiments_org_owner_activity_live
            ON experiments (
                org_id,
                owner_user_id,
                last_activity_at DESC NULLS LAST,
                id ASC
            )
            WHERE deleted_at IS NULL
            """
        )
        # Attribution discovery scan: who created this task by Clerk
        # user id? Partial on ``deleted_at IS NULL AND created_by_user_id
        # IS NOT NULL`` so the index is tight and the predicate matches
        # the discovery query exactly.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_tasks_org_created_by_live
            ON tasks (org_id, created_by_user_id)
            WHERE deleted_at IS NULL AND created_by_user_id IS NOT NULL
            """
        )
        # Legacy Mine fallback predicate: case-insensitive match on the
        # task ``user`` column (the Harbor submitter handle). Functional
        # index on ``lower("user")`` so the EXISTS subquery rides an
        # index instead of seq-scanning ``tasks``.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_tasks_org_lower_user_live
            ON tasks (org_id, lower("user"))
            WHERE deleted_at IS NULL
            """
        )
        # Legacy Mine fallback predicate: GitHub username carried in the
        # ``tags`` JSONB blob. Functional index on
        # ``lower(tags ->> 'github_username')`` so the EXISTS subquery
        # rides an index for the GitHub-identity branch of attribution.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_tasks_org_lower_github_tag_live
            ON tasks (org_id, lower((tags ->> 'github_username')))
            WHERE deleted_at IS NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_tasks_org_lower_github_tag_live"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_tasks_org_lower_user_live")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_tasks_org_created_by_live")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_experiments_org_owner_activity_live"
        )
