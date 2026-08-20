"""Drop the retired trial analysis log.

Revision ID: analysislog02
Revises: expmodelrename01, trajsum_002
Create Date: 2026-08-20 15:20:00.000000
"""

from typing import Sequence, Union

from alembic import op

from oddish.db.migration_locks import run_with_lock_retry

revision: str = "analysislog02"
down_revision: Union[str, Sequence[str], None] = (
    "expmodelrename01",
    "trajsum_002",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    run_with_lock_retry(
        lambda: op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS analysis_log"),
        table_name="trials",
    )


def downgrade() -> None:
    run_with_lock_retry(
        lambda: op.execute(
            "ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis_log TEXT"
        ),
        table_name="trials",
    )
