"""add SKIPPED to the jobstatus enum (trial status)

Baseline-gate-cancelled trials become a first-class ``SKIPPED`` status instead
of ``FAILED`` + a sentinel error_message, so the UI shows "Skipped" (not an
error) and they're excluded from failure/pass metrics (they never ran).

The ``jobstatus`` PG enum uses the enum *member names* (uppercase) as labels
(PENDING/QUEUED/.../FAILED/RETRYING), so we add ``'SKIPPED'``. No backfill:
pre-existing gate-cancelled rows stay ``FAILED`` + the legacy sentinel message.

``ALTER TYPE ... ADD VALUE`` MUST run outside a transaction, and the new value
MUST NOT be used in the same migration. Mirrors qa01 / TASK_EXPAND precedents.

Revision ID: skip01_add_skipped
Revises: api_key_creator_role_001
Create Date: 2026-07-03 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "skip01_add_skipped"
down_revision: Union[str, Sequence[str], None] = "cost_settle_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'SKIPPED'")


def downgrade() -> None:
    # Postgres can't cleanly drop enum values; 'SKIPPED' stays after downgrade.
    # Mirrors every other enum-add migration.
    pass
