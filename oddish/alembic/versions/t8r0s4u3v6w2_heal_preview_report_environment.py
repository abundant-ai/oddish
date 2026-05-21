"""heal preview DBs that missed report_task_versions.environment

Revision ID: t8r0s4u3v6w2
Revises: s7q9r3t2u5v1
Create Date: 2026-05-21 02:30:00.000000

``r5p9q8s2t1u4`` carries a late-add ``ALTER TABLE report_task_versions
ADD COLUMN IF NOT EXISTS environment`` inside its idempotent re-run
branch. On stranded preview DBs (alembic_version stuck at
``r5p9q8s2t1u4`` after the rebase) alembic never re-executes that body,
so the column never lands and ``/reports`` queries 500 with
"column report_task_versions.environment does not exist".

Sibling to ``s7q9r3t2u5v1`` -- this just covers the column the prior
heal step didn't know about yet. No-op on prod via IF NOT EXISTS.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "t8r0s4u3v6w2"
down_revision: Union[str, Sequence[str], None] = "s7q9r3t2u5v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE report_task_versions "
        "ADD COLUMN IF NOT EXISTS environment VARCHAR(32)"
    )


def downgrade() -> None:
    # Owned by ``r5p9q8s2t1u4``; nothing to undo here.
    pass
