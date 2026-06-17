"""drop_experiment_dispatch_log

Reverts a4b5c6d7e8f0. The repository_dispatch emitter has been removed
(revert of #206), so its single-fire idempotency ledger is no longer
used. Drops the table and its index. The add migration is kept so the
revision history stays linear and any environment can upgrade through it.

Revision ID: c1d2e3f4a5b6
Revises: a4b5c6d7e8f0
Create Date: 2026-06-08 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_experiment_dispatch_log_repo_at")
    op.execute("DROP TABLE IF EXISTS experiment_dispatch_log")


def downgrade() -> None:
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
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experiment_dispatch_log_repo_at
            ON experiment_dispatch_log (target_repo, dispatched_at DESC)
        """
    )
