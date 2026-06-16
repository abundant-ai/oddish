"""add QA to worker_job_kind enum

Trajectory analysis is now a single task-level QA job. The old ``VERDICT``
worker-job kind becomes ``QA`` (it classifies every trial and then synthesizes
the verdict in one job). This migration only *adds* the new ``QA`` value;
``qa02_migrate_verdict_to_qa`` repoints existing rows.

``ALTER TYPE ... ADD VALUE`` MUST run outside a transaction, and the new value
MUST NOT be used in the same migration (Postgres raises "unsafe use of new
value"). Mirrors the TASK_EXPAND / TAG_PROJECT precedents.

Revision ID: qa01_add_qa_kind
Revises: qslot01_locked_at
Create Date: 2026-06-15 23:10:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "qa01_add_qa_kind"
down_revision: Union[str, Sequence[str], None] = "qslot01_locked_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'QA'")


def downgrade() -> None:
    # Postgres does not support dropping enum values cleanly; the 'QA' label
    # stays on the enum after downgrade. Mirrors every other enum-add migration.
    pass
