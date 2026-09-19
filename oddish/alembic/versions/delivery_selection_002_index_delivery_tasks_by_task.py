"""Index delivery_tasks by task.

Revision ID: delivery_selection_002
Revises: delivery_selection_001

The task browser's "delivered to" filter asks, per task, whether a
finalized delivery contains it (an ``EXISTS`` over ``delivery_tasks``).
The table was indexed only by delivery, so that probe scanned it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "delivery_selection_002"
down_revision: Union[str, Sequence[str], None] = "delivery_selection_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_exists(table: str, name: str) -> bool:
    return name in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)
    }


def upgrade() -> None:
    if not _index_exists("delivery_tasks", "idx_delivery_tasks_task_id"):
        op.create_index("idx_delivery_tasks_task_id", "delivery_tasks", ["task_id"])


def downgrade() -> None:
    op.drop_index("idx_delivery_tasks_task_id", table_name="delivery_tasks")
