"""retire pending job status

``JobStatus.PENDING`` was never assigned at runtime for trials, analyses, or
verdicts -- new trials are created as ``QUEUED`` and the analysis/verdict
columns start as ``NULL`` until a handler sets ``QUEUED``. Only the
``trials.status`` column ever defaulted to ``PENDING``, and that default
was a dead code path since ``q.enqueue_trials_for_task`` assigns the status
explicitly. Drop the dead state:

1. Rewrite any legacy rows that still carry ``PENDING`` to ``QUEUED`` (for
   ``trials.status`` and ``trials.analysis_status``) or ``QUEUED`` (for
   ``tasks.verdict_status``).
2. Flip the ``trials.status`` column default to ``QUEUED`` so Postgres-side
   inserts match the Python-side default.

The ``jobstatus`` Postgres enum type still keeps its ``PENDING`` value (we
can't remove enum values without recreating the type and every column that
references it, which isn't worth the blast radius for a value nothing will
ever write again). Python no longer exposes the variant, so it's fully
retired at the application layer.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-24 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE trials SET status = 'QUEUED' WHERE status = 'PENDING'")
    op.execute(
        "UPDATE trials SET analysis_status = 'QUEUED' "
        "WHERE analysis_status = 'PENDING'"
    )
    op.execute(
        "UPDATE tasks SET verdict_status = 'QUEUED' "
        "WHERE verdict_status = 'PENDING'"
    )

    # Match the Python-side default in ``TrialModel.status``. The prior
    # default ``'PENDING'::jobstatus`` was set by
    # ``m8n9o0p1q2r3_normalize_status_enums_for_legacy_dbs``.
    op.execute("ALTER TABLE trials ALTER COLUMN status SET DEFAULT 'QUEUED'")


def downgrade() -> None:
    # Not reversible: the original PENDING rows are indistinguishable from
    # rows that were legitimately QUEUED at any point.
    op.execute("ALTER TABLE trials ALTER COLUMN status SET DEFAULT 'PENDING'")
