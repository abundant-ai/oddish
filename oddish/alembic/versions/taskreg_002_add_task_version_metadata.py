"""Add runtime + descriptive-snapshot columns to task_versions.

Revision ID: taskreg_002
Revises: taskreg_001
"""

from __future__ import annotations

from alembic import op

revision = "taskreg_002"
down_revision = "taskreg_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # AccessExclusiveLock is only held briefly (nullable columns, no
    # defaults -- no table rewrite), but a long-running reader on
    # task_versions could otherwise queue every writer behind this DDL.
    # Fail fast instead.
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        """
        ALTER TABLE task_versions
            ADD COLUMN IF NOT EXISTS allow_internet BOOLEAN,
            ADD COLUMN IF NOT EXISTS cpus INTEGER,
            ADD COLUMN IF NOT EXISTS memory_mb INTEGER,
            ADD COLUMN IF NOT EXISTS storage_mb INTEGER,
            ADD COLUMN IF NOT EXISTS gpus INTEGER,
            ADD COLUMN IF NOT EXISTS gpu_types TEXT[],
            ADD COLUMN IF NOT EXISTS agent_timeout_sec INTEGER,
            ADD COLUMN IF NOT EXISTS verifier_timeout_sec INTEGER,
            ADD COLUMN IF NOT EXISTS build_timeout_sec INTEGER,
            ADD COLUMN IF NOT EXISTS description_snapshot TEXT,
            ADD COLUMN IF NOT EXISTS category_snapshot VARCHAR(128),
            ADD COLUMN IF NOT EXISTS topic_tags_snapshot TEXT[],
            ADD COLUMN IF NOT EXISTS expert_time_hours_snapshot NUMERIC(6, 2)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE task_versions
            DROP COLUMN IF EXISTS allow_internet,
            DROP COLUMN IF EXISTS cpus,
            DROP COLUMN IF EXISTS memory_mb,
            DROP COLUMN IF EXISTS storage_mb,
            DROP COLUMN IF EXISTS gpus,
            DROP COLUMN IF EXISTS gpu_types,
            DROP COLUMN IF EXISTS agent_timeout_sec,
            DROP COLUMN IF EXISTS verifier_timeout_sec,
            DROP COLUMN IF EXISTS build_timeout_sec,
            DROP COLUMN IF EXISTS description_snapshot,
            DROP COLUMN IF EXISTS category_snapshot,
            DROP COLUMN IF EXISTS topic_tags_snapshot,
            DROP COLUMN IF EXISTS expert_time_hours_snapshot
        """
    )
