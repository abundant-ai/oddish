"""Add append-only task_upload_events table.

Revision ID: taskreg_003
Revises: taskreg_002

Each FK is added NOT VALID and validated in its own autocommit statement, one
referenced table at a time. Doing both in one transaction (the original form)
holds a ShareRowExclusiveLock on tasks and one on task_versions
simultaneously -- the same shape that deadlocked exp_trials_join_001 against
concurrent app traffic touching those tables in the opposite order.
lock_timeout makes a contended step fail fast instead of queueing ahead of
worker DML on tasks/task_versions.
"""

from __future__ import annotations

from alembic import op

revision = "taskreg_003"
down_revision = "taskreg_002"
branch_labels = None
depends_on = None


def _autocommit(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sql)


def _add_fk_not_valid(name: str, column: str, ref_table: str, on_delete: str) -> None:
    _autocommit(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = '{name}'
            ) THEN
                ALTER TABLE task_upload_events
                    ADD CONSTRAINT {name}
                    FOREIGN KEY ({column}) REFERENCES {ref_table}(id)
                    ON DELETE {on_delete}
                    NOT VALID;
            END IF;
        END$$;
        """
    )


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

    # Session-level; persists across the autocommit steps on this connection.
    _autocommit("SET lock_timeout = '3s'")

    # One referenced table's lock per statement/transaction, never both at once.
    _add_fk_not_valid(
        name="fk_task_upload_events_task",
        column="task_id",
        ref_table="tasks",
        on_delete="CASCADE",
    )
    _autocommit(
        "ALTER TABLE task_upload_events VALIDATE CONSTRAINT fk_task_upload_events_task"
    )
    _add_fk_not_valid(
        name="fk_task_upload_events_version",
        column="task_version_id",
        ref_table="task_versions",
        on_delete="SET NULL",
    )
    _autocommit(
        "ALTER TABLE task_upload_events VALIDATE CONSTRAINT fk_task_upload_events_version"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_task_upload_events_task_created "
        "ON task_upload_events (task_id, created_at DESC)"
    )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_task_upload_events_task_created"
        )
    op.execute("DROP TABLE IF EXISTS task_upload_events")
