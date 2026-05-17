"""move nop oracle to dedicated queue

Revision ID: 637c45e5ac80
Revises: j2e3f4a5b6c7
Create Date: 2026-05-16 13:32:41.242793

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "637c45e5ac80"
down_revision: Union[str, Sequence[str], None] = "j2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE worker_jobs AS wj
        SET queue_key = 'nop_oracle'
        FROM trials AS t
        WHERE wj.kind = 'TRIAL'
          AND wj.subject_table = 'trials'
          AND wj.subject_id = t.id
          AND LOWER(COALESCE(t.agent, '')) IN ('nop', 'oracle')
          AND wj.status IN ('QUEUED', 'RETRYING', 'BLOCKED')
        """
    )
    op.execute(
        """
        UPDATE trials
        SET queue_key = 'nop_oracle'
        WHERE LOWER(COALESCE(agent, '')) IN ('nop', 'oracle')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE worker_jobs AS wj
        SET queue_key = 'default'
        FROM trials AS t
        WHERE wj.kind = 'TRIAL'
          AND wj.subject_table = 'trials'
          AND wj.subject_id = t.id
          AND LOWER(COALESCE(t.agent, '')) IN ('nop', 'oracle')
          AND wj.status IN ('QUEUED', 'RETRYING', 'BLOCKED')
        """
    )
    op.execute(
        """
        UPDATE trials
        SET queue_key = 'default'
        WHERE LOWER(COALESCE(agent, '')) IN ('nop', 'oracle')
        """
    )
