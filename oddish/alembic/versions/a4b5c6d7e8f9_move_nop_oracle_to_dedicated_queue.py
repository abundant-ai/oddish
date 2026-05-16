"""move_nop_oracle_to_dedicated_queue

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "z3a4b5c6d7e8"
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
