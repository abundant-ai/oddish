"""repoint VERDICT worker_jobs rows to the unified QA kind

Second half of the analysis/verdict -> single QA-job rename. With ``QA`` now
present on the enum (``qa01_add_qa_kind``), move every existing ``VERDICT``
row to ``QA``. The old per-task ``VERDICT`` job and the per-trial ``ANALYSIS``
job collapsed into one task-level ``QA`` job, so ``VERDICT`` becomes a dead
enum label (Postgres can't drop it; no rows reference it afterwards).

``ANALYSIS`` rows are intentionally left as-is: they are terminal historical
rows, and any rare in-flight one is drained by the transitional ANALYSIS
handler across the deploy.

Revision ID: qa02_verdict_to_qa
Revises: qa01_add_qa_kind
Create Date: 2026-06-15 23:11:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "qa02_verdict_to_qa"
down_revision: Union[str, Sequence[str], None] = "qa01_add_qa_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE worker_jobs SET kind = 'QA' WHERE kind::text = 'VERDICT'")


def downgrade() -> None:
    op.execute("UPDATE worker_jobs SET kind = 'VERDICT' WHERE kind::text = 'QA'")
