"""add (org_id, finished_at) trials index for the org-wide quota sums

Revision ID: org_quota_idx_001
Revises: taskapikey_oss_001
Create Date: 2026-07-06 00:00:00.000000

``sum_org_cost_usd`` filters ``org_id = ? AND finished_at > start_of_month``
and ``org_inflight_reserved_usd`` filters ``org_id = ? AND finished_at IS
NULL`` -- both run on every admission when an org cap is configured (shadow
and enforce). The existing ``idx_trials_org_billed_user_finished`` cannot seek
``finished_at`` for either (it sits behind an unconstrained
``billed_user_id``), so without this index each admission scans the org's
entire trial history. Not partial: the settled sum counts soft-deleted rows
(``include_deleted=True``), so the index must cover them too.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "org_quota_idx_001"
down_revision: Union[str, Sequence[str], None] = "taskapikey_oss_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trials_org_finished_at
            ON trials (org_id, finished_at)
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_trials_org_finished_at")
