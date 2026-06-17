"""widen task-derived ids

Revision ID: long_task_ids_001
Revises: expdesc_001
Create Date: 2026-06-17 11:05:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "long_task_ids_001"
down_revision: Union[str, Sequence[str], None] = "expdesc_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_constraints() -> None:
    op.execute(
        "ALTER TABLE tasks DROP CONSTRAINT IF EXISTS fk_tasks_current_version_id"
    )
    op.execute(
        "ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_current_version_id_fkey"
    )
    op.execute(
        "ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_superseded_by_trial_id_fkey"
    )
    op.execute(
        "ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_task_version_id_fkey"
    )
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_task_id_fkey")
    op.execute(
        "ALTER TABLE task_versions DROP CONSTRAINT IF EXISTS task_versions_task_id_fkey"
    )
    op.execute(
        "ALTER TABLE task_experiments DROP CONSTRAINT IF EXISTS task_experiments_task_id_fkey"
    )


def _add_constraints() -> None:
    op.execute(
        """
        ALTER TABLE task_experiments
        ADD CONSTRAINT task_experiments_task_id_fkey
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        ALTER TABLE task_versions
        ADD CONSTRAINT task_versions_task_id_fkey
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        ALTER TABLE tasks
        ADD CONSTRAINT fk_tasks_current_version_id
        FOREIGN KEY (current_version_id) REFERENCES task_versions(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT trials_task_id_fkey
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT trials_task_version_id_fkey
        FOREIGN KEY (task_version_id) REFERENCES task_versions(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT trials_superseded_by_trial_id_fkey
        FOREIGN KEY (superseded_by_trial_id) REFERENCES trials(id) ON DELETE SET NULL
        """
    )


def upgrade() -> None:
    """Allow task names up to 64 characters to fit generated task ids."""
    op.execute(
        "LOCK TABLE tasks, task_experiments, task_versions, trials "
        "IN ACCESS EXCLUSIVE MODE"
    )
    _drop_constraints()

    op.execute("ALTER TABLE tasks ALTER COLUMN id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE task_experiments ALTER COLUMN task_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE task_versions ALTER COLUMN task_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE task_versions ALTER COLUMN id TYPE VARCHAR(160)")
    op.execute("ALTER TABLE tasks ALTER COLUMN current_version_id TYPE VARCHAR(160)")
    op.execute("ALTER TABLE trials ALTER COLUMN id TYPE VARCHAR(160)")
    op.execute("ALTER TABLE trials ALTER COLUMN task_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE trials ALTER COLUMN task_version_id TYPE VARCHAR(160)")
    op.execute(
        "ALTER TABLE trials ALTER COLUMN superseded_by_trial_id TYPE VARCHAR(160)"
    )

    _add_constraints()


def downgrade() -> None:
    """Restore the previous id lengths."""
    op.execute(
        "LOCK TABLE tasks, task_experiments, task_versions, trials "
        "IN ACCESS EXCLUSIVE MODE"
    )
    _drop_constraints()

    op.execute(
        "ALTER TABLE trials ALTER COLUMN superseded_by_trial_id TYPE VARCHAR(128)"
    )
    op.execute("ALTER TABLE trials ALTER COLUMN task_version_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE trials ALTER COLUMN task_id TYPE VARCHAR(64)")
    op.execute("ALTER TABLE trials ALTER COLUMN id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE tasks ALTER COLUMN current_version_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE task_versions ALTER COLUMN id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE task_versions ALTER COLUMN task_id TYPE VARCHAR(64)")
    op.execute("ALTER TABLE task_experiments ALTER COLUMN task_id TYPE VARCHAR(64)")
    op.execute("ALTER TABLE tasks ALTER COLUMN id TYPE VARCHAR(64)")

    _add_constraints()
