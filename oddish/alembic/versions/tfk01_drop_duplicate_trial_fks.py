"""drop duplicate trial links

Revision ID: tfk01_drop_duplicate_trial_fks
Revises: apk01dropfk
Create Date: 2026-06-25
"""

from typing import Sequence, Union

from alembic import op


revision: str = "tfk01_drop_duplicate_trial_fks"
down_revision: Union[str, Sequence[str], None] = "apk01dropfk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_experiment_id_fkey")
    op.execute(
        "ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_task_version_id_fkey"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT trials_experiment_id_fkey
        FOREIGN KEY (experiment_id) REFERENCES experiments (id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT trials_task_version_id_fkey
        FOREIGN KEY (task_version_id) REFERENCES task_versions (id) ON DELETE SET NULL
        """
    )
