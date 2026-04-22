"""add task version expansion

Adds metadata columns that track whether a task version has been
expanded from its tarball into a per-file S3 tree at
``tasks/{task_id}/v{N}-files/``, and registers the ``TASK_EXPAND``
worker-job kind that does the expansion.

The expanded layout lets the dashboard list task files with a single
``ListObjectsV2`` call and fetch content via per-file presigned URLs
(the same fast path trial files already use). The archive at
``tasks/{task_id}/v{N}/.oddish-task.tar.gz`` remains the canonical,
immutable artifact; the expanded tree is a derived cache.

Revision ID: c4b5a6d7e8f9
Revises: f9a0b1c2d3e4
Create Date: 2026-04-21 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4b5a6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # task_versions: per-row expansion metadata
    # ------------------------------------------------------------------
    op.add_column(
        "task_versions",
        sa.Column(
            "expanded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "task_versions",
        sa.Column(
            "expanded_manifest_key",
            sa.Text,
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # worker_job_kind: add TASK_EXPAND
    #
    # ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction in
    # older Postgres versions. Alembic autocommit ensures the statement
    # runs standalone. ``IF NOT EXISTS`` keeps the migration idempotent
    # for databases that were manually patched.
    # ------------------------------------------------------------------
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'TASK_EXPAND'"
        )


def downgrade() -> None:
    op.drop_column("task_versions", "expanded_manifest_key")
    op.drop_column("task_versions", "expanded_at")

    # Postgres doesn't support removing enum values cleanly; the
    # ``TASK_EXPAND`` label stays on the enum type after downgrade.
    # This matches how other enum-adding migrations in this project
    # (and every other Postgres-backed Alembic project) handle it.
