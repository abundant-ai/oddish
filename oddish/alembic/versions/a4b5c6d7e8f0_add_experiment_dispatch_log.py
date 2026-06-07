"""add_experiment_dispatch_log

Single-fire idempotency ledger for repository_dispatch callbacks emitted
when an experiment reaches the all-tasks-terminal state. The dispatcher
in oddish.integrations.github.dispatch claims this row via
INSERT ... ON CONFLICT DO NOTHING before POSTing to GitHub, so a re-fired
verdict hook (or cleanup sweep) can't double-post.

Revision ID: a4b5c6d7e8f0
Revises: 74a0eab3e564
Create Date: 2026-06-08 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a4b5c6d7e8f0"
down_revision: Union[str, Sequence[str], None] = "74a0eab3e564"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_dispatch_log (
            experiment_id   VARCHAR(64) PRIMARY KEY,
            event_type      VARCHAR(128) NOT NULL,
            target_repo     VARCHAR(255) NOT NULL,
            dispatched_at   TIMESTAMPTZ NOT NULL
        )
        """
    )
    # Operational read path: "show me everything dispatched at <repo> in
    # the last hour". Cheap, indexed.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experiment_dispatch_log_repo_at
            ON experiment_dispatch_log (target_repo, dispatched_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_experiment_dispatch_log_repo_at")
    op.execute("DROP TABLE IF EXISTS experiment_dispatch_log")
