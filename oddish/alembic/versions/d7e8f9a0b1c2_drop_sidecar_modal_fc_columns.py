"""drop sidecar modal_function_call_id columns from trials/tasks

Scheduling state -- including Modal function-call ids used for remote
cancellation -- now lives exclusively on ``worker_jobs``. The domain
columns this migration drops were denormalized sidecars from the
pre-unification era:

- ``trials.modal_function_call_id``
- ``trials.analysis_modal_function_call_id``
- ``tasks.verdict_modal_function_call_id``

Everything that used to read/write them was ported in the same PR:

- Handler claim/start/finish no longer copies FCs to the domain row.
- ``cancel_tasks_runs`` harvests FCs from ``worker_jobs.RETURNING``
  (single source of truth) -- no more domain-row reads.
- Retry endpoints (``retry_trial_core`` / ``rerun_*``) do not reset
  these columns because the new worker_jobs row carries its own FC.
- Cleanup sweep no longer references the domain columns.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-04-21 00:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "c6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS modal_function_call_id")
    op.execute(
        "ALTER TABLE trials DROP COLUMN IF EXISTS analysis_modal_function_call_id"
    )
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict_modal_function_call_id")


def downgrade() -> None:
    # Add the columns back as nullable strings. They'll be empty on
    # existing rows -- callers that relied on them need
    # ``worker_jobs.modal_function_call_id`` instead; this downgrade is
    # here purely for migration reversibility, not for restoring data.
    op.add_column(
        "trials",
        sa.Column("modal_function_call_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "trials",
        sa.Column(
            "analysis_modal_function_call_id", sa.String(length=128), nullable=True
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "verdict_modal_function_call_id", sa.String(length=128), nullable=True
        ),
    )
