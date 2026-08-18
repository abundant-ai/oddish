"""Enforce the published-verdict state invariant.

``tasks.verdict`` stores the last successfully published result even while a
replacement QA pass is queued or running. A payload without a status makes the
result disappear from status-based readers; a payload paired with FAILED keeps
a result that the failed replacement should have invalidated.

Revision ID: verdict_state_001
Revises: drop_prompt_registry_001
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "verdict_state_001"
down_revision: Union[str, Sequence[str], None] = "drop_prompt_registry_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "ck_tasks_published_verdict_status"


def _constraint_exists(bind: sa.engine.Connection) -> bool:
    return _CONSTRAINT in {
        item["name"] for item in sa.inspect(bind).get_check_constraints("tasks")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if _constraint_exists(bind):
        return

    # NOT VALID starts protecting concurrent writes without first scanning the
    # table. Repair historical rows under that protection, then validate.
    # SQLAlchemy's JSONB type writes Python None as JSON null, so both null
    # representations are unpublished and valid.
    op.execute(
        f"""
        ALTER TABLE tasks
        ADD CONSTRAINT {_CONSTRAINT}
        CHECK (
            verdict IS NULL OR verdict = 'null'::jsonb OR
            (verdict_status IS NOT NULL AND verdict_status <> 'FAILED')
        ) NOT VALID
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET verdict_status = 'SUCCESS',
            verdict_error = NULL,
            verdict_started_at = NULL
        WHERE verdict IS NOT NULL
          AND verdict <> 'null'::jsonb
          AND verdict_status IS NULL
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET verdict = NULL
        WHERE verdict IS NOT NULL
          AND verdict <> 'null'::jsonb
          AND verdict_status = 'FAILED'
        """
    )
    op.execute(f"ALTER TABLE tasks VALIDATE CONSTRAINT {_CONSTRAINT}")


def downgrade() -> None:
    bind = op.get_bind()
    if _constraint_exists(bind):
        op.drop_constraint(_CONSTRAINT, "tasks", type_="check")
