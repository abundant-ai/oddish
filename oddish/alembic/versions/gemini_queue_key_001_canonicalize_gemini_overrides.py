"""canonicalize gemini/ concurrency overrides onto google/

Revision ID: gemini_queue_key_001
Revises: quota_pause_status_001
Create Date: 2026-08-27

Every Gemini spelling now normalizes to one queue key, ``google/<id>``. This
table is keyed by queue key, so it follows the queue-key layer, not the model-id
layer -- a Gemini trial stores the model id ``gemini/<id>`` but queues on
``google/<id>``, and an override governs the bucket.

``get_model_concurrency_overrides`` reads this table by the *normalized* key, so
a row still stored under ``gemini/<id>`` would stop matching after the deploy and
the queue would fall back to the deploy-time default. An admin override that
disables or throttles a queue must never disappear that way: the read path
already fails closed at zero rather than risk reopening a disabled queue, and
this migration keeps that promise for the rename.

On conflict the LOWER of the two limits wins. The migration can therefore only
tighten a limit, never raise one.

``vertex_ai/`` rows are left alone. Vertex AI keeps its own queue key, so those
rows still govern the bucket they were written for.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "gemini_queue_key_001"
down_revision: Union[str, Sequence[str], None] = "quota_pause_status_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fold a legacy ``gemini/<id>`` row into ``google/<id>``, keeping the lower
    # limit when both exist.
    op.execute(
        """
        INSERT INTO model_concurrency_overrides
            (queue_key, concurrency_limit, updated_at)
        SELECT 'google/' || substring(queue_key from 8),
               MIN(concurrency_limit),
               NOW()
        FROM   model_concurrency_overrides
        WHERE  queue_key LIKE 'gemini/%'
        -- Group so the statement cannot propose the same target key twice,
        -- which ON CONFLICT DO UPDATE rejects.
        GROUP  BY 'google/' || substring(queue_key from 8)
        ON CONFLICT (queue_key) DO UPDATE
        SET concurrency_limit = LEAST(
                model_concurrency_overrides.concurrency_limit,
                EXCLUDED.concurrency_limit
            ),
            updated_at = NOW()
        """
    )
    op.execute(
        "DELETE FROM model_concurrency_overrides WHERE queue_key LIKE 'gemini/%'"
    )


def downgrade() -> None:
    # The original spelling is not recoverable: the rows were merged, and the
    # pre-merge limits are gone. Copy each canonical row back to the ``gemini/``
    # spelling so a rollback leaves both keys governed rather than one
    # unthrottled.
    op.execute(
        """
        INSERT INTO model_concurrency_overrides
            (queue_key, concurrency_limit, updated_at)
        SELECT 'gemini/' || substring(queue_key from 8),
               concurrency_limit,
               NOW()
        FROM   model_concurrency_overrides
        WHERE  queue_key LIKE 'google/%'
        ON CONFLICT (queue_key) DO NOTHING
        """
    )
