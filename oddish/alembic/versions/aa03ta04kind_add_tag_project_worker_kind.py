"""add TAG_PROJECT to worker_job_kind enum

``ALTER TYPE ... ADD VALUE`` MUST run outside a transaction, and the new
value MUST NOT be used in the same migration (Postgres raises "unsafe use
of new value"). Mirrors the TASK_EXPAND precedent in
``c4b5a6d7e8f9_add_task_version_expansion``.

Revision ID: aa03ta04kind
Revises: trial_is_probe_001
Create Date: 2026-06-06 12:15:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "aa03ta04kind"
down_revision: Union[str, Sequence[str], None] = "trial_is_probe_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'TAG_PROJECT'")


def downgrade() -> None:
    # Postgres does not support dropping enum values cleanly; the
    # 'TAG_PROJECT' label stays on the enum after downgrade. Mirrors
    # how every other enum-add migration in this project handles this.
    pass
