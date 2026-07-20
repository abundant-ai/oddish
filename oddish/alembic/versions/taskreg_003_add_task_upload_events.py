"""Add append-only task_upload_events table.

Revision ID: taskreg_003
Revises: taskreg_002
"""

from __future__ import annotations

from alembic import op

revision = "taskreg_003"
down_revision = "taskreg_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_upload_events (
            id VARCHAR(64) PRIMARY KEY,
            task_id VARCHAR(128) NOT NULL,
            task_version_id VARCHAR(160),
            created_version BOOLEAN NOT NULL,
            content_hash VARCHAR(128),
            source_repo VARCHAR(255),
            source_commit VARCHAR(64),
            source_ref VARCHAR(255),
            source_path TEXT,
            ci_provider VARCHAR(64),
            ci_run_id VARCHAR(64),
            ci_run_url TEXT,
            ci_pr_number INTEGER,
            uploader_is_ci BOOLEAN,
            uploader_user_id VARCHAR(64),
            uploader_cli_version VARCHAR(64),
            uploader_host VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # FKs added separately as NOT VALID then validated, so this migration never
    # takes a long lock on tasks/task_versions during deploy.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_task_upload_events_task'
            ) THEN
                ALTER TABLE task_upload_events
                    ADD CONSTRAINT fk_task_upload_events_task
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_task_upload_events_version'
            ) THEN
                ALTER TABLE task_upload_events
                    ADD CONSTRAINT fk_task_upload_events_version
                    FOREIGN KEY (task_version_id) REFERENCES task_versions(id)
                    ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END$$;
        """
    )
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TABLE task_upload_events VALIDATE CONSTRAINT fk_task_upload_events_task"
        )
        op.execute(
            "ALTER TABLE task_upload_events VALIDATE CONSTRAINT fk_task_upload_events_version"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_task_upload_events_task_created "
            "ON task_upload_events (task_id, created_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_task_upload_events_task_created"
        )
    op.execute("DROP TABLE IF EXISTS task_upload_events")
