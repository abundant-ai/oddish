"""add CANCELLED to the taskstatus enum (task status)

A user- or quota-cancelled run becomes a first-class ``CANCELLED`` task status
instead of ``FAILED`` (or ``COMPLETED`` when a kept verdict forced the
COMPLETED override). Status now reflects the execution outcome — stopped, not
broken — while the verdict columns independently keep the judgment, so a kept
SUCCESS verdict stays valid on a cancelled task.

The ``taskstatus`` PG enum uses the enum *member names* (uppercase) as labels
(PENDING/RUNNING/.../FAILED), so we add ``'CANCELLED'``. No backfill:
pre-existing cancelled rows stay ``FAILED`` + the cancel message.

``ALTER TYPE ... ADD VALUE`` MUST run outside a transaction, and the new value
MUST NOT be used in the same migration. Mirrors skip01 / qa01 precedents.

Revision ID: cancel01_add_cancelled
Revises: drop_prompt_registry_001
Create Date: 2026-08-08 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "cancel01_add_cancelled"
down_revision: Union[str, Sequence[str], None] = "drop_prompt_registry_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'CANCELLED'")


def downgrade() -> None:
    # Postgres can't cleanly drop enum values; 'CANCELLED' stays after
    # downgrade. Mirrors every other enum-add migration.
    pass
