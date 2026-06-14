"""add probe-runs listing index

Revision ID: probe_runs_idx_001
Revises: merge_tag_run_probe_heads
Create Date: 2026-06-12 00:00:00.000000

Backs ``oddish.core.experiments.list_org_probes_core`` (the QA "Probe runs"
listing). The query scans the org's probe trials and, per task, counts them
and ranks newest-first via window functions. This partial composite lets
Postgres seek by ``org_id`` and walk each task's trials in
``created_at DESC`` order without a separate sort, and keeps the index tight
by only covering probe rows.

Created ``CONCURRENTLY`` + ``IF NOT EXISTS`` so it is safe against a busy
database and idempotent on a fresh DB. ``CONCURRENTLY`` cannot run inside a
transaction, hence the ``autocommit_block``.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "probe_runs_idx_001"
down_revision: Union[str, Sequence[str], None] = "merge_tag_run_probe_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_trials_probe_org_task_created
            ON trials (org_id, task_id, created_at DESC)
            WHERE is_probe
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_trials_probe_org_task_created"
        )
