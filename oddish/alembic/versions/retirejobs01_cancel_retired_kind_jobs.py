"""Cancel in-flight worker_jobs of the retired analysis kinds.

Revision ID: retirejobs01
Revises: shadowexp01
Create Date: 2026-08-18

QA, audits, and analyzer work run as trials now. No handler claims the old
kinds anymore (workers only claim registered kinds), so a live row of one of
these kinds can never run again -- it would sit QUEUED forever. Cancel them
with a sentinel message; the cleanup sweep's VERDICT_PENDING healer then
restarts the interrupted work as analysis trials, so no task stays stuck.
The enum values themselves stay so historical rows keep deserializing.

Idempotent: the WHERE clause only matches live rows, and a re-run matches
nothing. On a fresh database the table is empty and this is a no-op.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "retirejobs01"
down_revision: Union[str, Sequence[str], None] = "shadowexp01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Bound waiting on row locks a still-draining worker may briefly hold.
    op.execute("SET lock_timeout = '8s'")
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
    # The cancelled rows carry a sentinel error_message, but resurrecting
    # them would hand jobs to handlers the downgraded code re-registers with
    # unknowable staleness. Leave them cancelled; downgrade only unwinds the
    # schema chain.
    pass
