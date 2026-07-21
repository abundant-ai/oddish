"""Add descriptive metadata columns to tasks.

Revision ID: taskreg_001
Revises: analyzers_008
"""

from __future__ import annotations

from alembic import op

revision = "taskreg_001"
down_revision = "analyzers_008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_metadata_source') THEN
                CREATE TYPE task_metadata_source AS ENUM ('CLIENT', 'BACKFILL');
            END IF;
        END$$;
        """
    )
    # AccessExclusiveLock is only held briefly (nullable columns, no
    # defaults -- no table rewrite), but a long-running reader on tasks could
    # otherwise queue every writer behind this DDL. Fail fast instead.
    op.execute("SET lock_timeout = '3s'")
    op.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS description TEXT,
            ADD COLUMN IF NOT EXISTS category VARCHAR(128),
            ADD COLUMN IF NOT EXISTS category_raw VARCHAR(128),
            ADD COLUMN IF NOT EXISTS author_name VARCHAR(255),
            ADD COLUMN IF NOT EXISTS author_email VARCHAR(255),
            ADD COLUMN IF NOT EXISTS author_organization VARCHAR(255),
            ADD COLUMN IF NOT EXISTS expert_time_hours NUMERIC(6, 2),
            ADD COLUMN IF NOT EXISTS metadata_source task_metadata_source,
            ADD COLUMN IF NOT EXISTS metadata_updated_at TIMESTAMPTZ
        """
    )
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_category "
            "ON tasks (category) WHERE category IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_tasks_category")
    op.execute(
        """
        ALTER TABLE tasks
            DROP COLUMN IF EXISTS description,
            DROP COLUMN IF EXISTS category,
            DROP COLUMN IF EXISTS category_raw,
            DROP COLUMN IF EXISTS author_name,
            DROP COLUMN IF EXISTS author_email,
            DROP COLUMN IF EXISTS author_organization,
            DROP COLUMN IF EXISTS expert_time_hours,
            DROP COLUMN IF EXISTS metadata_source,
            DROP COLUMN IF EXISTS metadata_updated_at
        """
    )
    op.execute("DROP TYPE IF EXISTS task_metadata_source")
