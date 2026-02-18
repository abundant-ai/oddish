"""increase_id_lengths

Revision ID: 3e83b4d6a123
Revises: 7270853bccfe
Create Date: 2026-01-15 13:30:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "3e83b4d6a123"
down_revision: Union[str, Sequence[str], None] = "7270853bccfe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Drop the FK constraint
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_task_id_fkey")

    # 2. Alter the column types
    op.execute("ALTER TABLE tasks ALTER COLUMN id TYPE VARCHAR(64)")
    op.execute("ALTER TABLE trials ALTER COLUMN task_id TYPE VARCHAR(64)")
    op.execute("ALTER TABLE trials ALTER COLUMN id TYPE VARCHAR(128)")

    # 3. Re-add the FK constraint
    op.execute(
        "ALTER TABLE trials "
        "ADD CONSTRAINT trials_task_id_fkey "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop FK
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_task_id_fkey")

    # Shrink columns (might fail if data is too long)
    op.execute("ALTER TABLE trials ALTER COLUMN id TYPE VARCHAR(16)")
    op.execute("ALTER TABLE trials ALTER COLUMN task_id TYPE VARCHAR(8)")
    op.execute("ALTER TABLE tasks ALTER COLUMN id TYPE VARCHAR(8)")

    # Re-add FK
    op.execute(
        "ALTER TABLE trials "
        "ADD CONSTRAINT trials_task_id_fkey "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"
    )
