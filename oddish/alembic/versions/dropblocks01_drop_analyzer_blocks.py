"""drop the analyzer_blocks table

Revision ID: dropblocks01
Revises: analysisspend01
Create Date: 2026-08-19 00:00:00.000000

The block pipeline is gone: trajectory summaries are written onto
``trials.trajectory_summary`` by the task's QA trial import, and both summary
routes read that column only. Before dropping the table, the newest SUCCESS
``trajectory_summary`` block per trial is copied into any empty mirror --
a summary that existed solely as an ``analyzer_blocks`` row must not vanish
with the table. Pre-cutover summaries survive with their original
schema/taxonomy stamps; the QA pipeline overwrites them on the next QA run.

Guarded by ``to_regclass``: a fresh database built by ``create_all`` never
has the table (the model is deleted), so both the backfill and the DROP are
no-ops there. The shared ``jobstatus`` enum type stays -- other tables use it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "dropblocks01"
down_revision: Union[str, Sequence[str], None] = "analysisspend01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '8s'")
    result = op.get_bind().execute(
        sa.text("SELECT to_regclass('analyzer_blocks') IS NOT NULL")
    )
    if result.scalar():
        op.execute(
            """
            UPDATE trials t
            SET    trajectory_summary = b.output
            FROM (
                SELECT DISTINCT ON (analyzer_id) analyzer_id, output
                FROM analyzer_blocks
                WHERE type = 'trajectory_summary'
                  AND status::text = 'SUCCESS'
                  AND output IS NOT NULL
                  AND deleted_at IS NULL
                ORDER BY analyzer_id, created_at DESC
            ) b
            WHERE t.id = b.analyzer_id
              AND t.trajectory_summary IS NULL
            """
        )
    op.execute("DROP TABLE IF EXISTS analyzer_blocks")


def downgrade() -> None:
    # Recreates the table at its final shape, without data. Not a hot-path
    # operation; plain (non-concurrent) index builds are fine here.
    op.execute("SET lock_timeout = '8s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS analyzer_blocks (
            id VARCHAR(64) PRIMARY KEY,
            analyzer_id VARCHAR(64),
            task_id VARCHAR(128),
            type VARCHAR(64) NOT NULL,
            key_prefix TEXT NOT NULL,
            llm_client_type VARCHAR(64) NOT NULL,
            prompt TEXT,
            prompt_key VARCHAR(128),
            prompt_version INTEGER,
            prompt_id VARCHAR(64),
            input JSONB,
            output JSONB,
            status jobstatus NOT NULL DEFAULT 'PENDING',
            error TEXT,
            job_started_at TIMESTAMPTZ,
            job_ended_at TIMESTAMPTZ,
            job_duration_seconds FLOAT,
            metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_analyzer_blocks_analyzer_id "
        "ON analyzer_blocks (analyzer_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_analyzer_blocks_task_id "
        "ON analyzer_blocks (task_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_analyzer_blocks_prompt_id "
        "ON analyzer_blocks (prompt_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyzer_blocks_analyzer_id_live "
        "ON analyzer_blocks (analyzer_id) WHERE deleted_at IS NULL"
    )
