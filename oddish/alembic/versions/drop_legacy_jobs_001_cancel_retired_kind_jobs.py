"""cancel worker_jobs of retired kinds

Revision ID: drop_legacy_jobs_001
Revises: shadow_exp_001
Create Date: 2026-08-07 00:00:00.000000

QA, audit, and analyzer work runs as trials now. Nothing handles the old
job kinds anymore, so live rows of those kinds can only fail with "no
handler registered". Cancel them. The enum values themselves stay so
historical rows keep deserializing.

Idempotent: the WHERE clause only matches live rows.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "drop_legacy_jobs_001"
down_revision: Union[str, Sequence[str], None] = "shadow_exp_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE worker_jobs
        SET    status = 'CANCELLED',
               finished_at = NOW(),
               error_message = 'pipeline removed: analysis runs as trials now',
               current_worker_id = NULL,
               current_queue_slot = NULL,
               modal_function_call_id = NULL
        WHERE  kind::text IN
               ('QA', 'VERDICT', 'ANALYSIS', 'QA_REVIEW',
                'ANALYZER', 'ANALYZER_BLOCK')
          AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
        """
    )


def downgrade() -> None:
    pass
