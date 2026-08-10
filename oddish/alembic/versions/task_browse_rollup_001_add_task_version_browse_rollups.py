"""Persist bounded task-browser summaries on task versions.

Revision ID: task_browse_rollup_001
Revises: verdict_state_001
"""

from __future__ import annotations

import json
from itertools import groupby

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "task_browse_rollup_001"
down_revision = "verdict_state_001"
branch_labels = None
depends_on = None

_SELECT_VERSION_IDS = sa.text(
    """
    SELECT DISTINCT task_version_id
    FROM trials
    WHERE task_version_id > :after
      AND deleted_at IS NULL
      AND superseded_by_trial_id IS NULL
      AND is_probe IS NOT TRUE
      AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')
    ORDER BY task_version_id
    LIMIT 100
    """
)
_SELECT_TRIALS = sa.text(
    """
    SELECT id, name, task_version_id, status, reward, error_message, agent, model,
           cost_usd, input_tokens, output_tokens, cache_tokens,
           cache_write_tokens, billed_user_id, started_at, finished_at, created_at
    FROM trials
    WHERE task_version_id = ANY(CAST(:version_ids AS text[]))
      AND deleted_at IS NULL
      AND superseded_by_trial_id IS NULL
      AND is_probe IS NOT TRUE
      AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')
    ORDER BY task_version_id, created_at, id
    """
)
_UPDATE_VERSION = sa.text(
    """
    UPDATE task_versions
    SET browse_last_run_at = :last_run_at,
        browse_rollup = CAST(:summary AS jsonb)
    WHERE id = :version_id
    """
)


def _backfill(bind: sa.engine.Connection) -> None:
    from oddish.core.task_browse_projection import build_task_version_browse_projection

    after = ""
    while version_ids := list(
        bind.execute(_SELECT_VERSION_IDS, {"after": after}).scalars()
    ):
        rows = bind.execute(_SELECT_TRIALS, {"version_ids": version_ids}).mappings()
        updates: list[dict] = []
        for version_id, version_rows in groupby(
            rows, key=lambda row: str(row["task_version_id"])
        ):
            last_run_at, summary = build_task_version_browse_projection(version_rows)
            updates.append(
                {
                    "version_id": version_id,
                    "last_run_at": last_run_at,
                    "summary": json.dumps(summary),
                }
            )
        bind.execute(_UPDATE_VERSION, updates)
        after = str(version_ids[-1])


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("task_versions")
    }
    if "browse_last_run_at" not in columns:
        op.add_column(
            "task_versions",
            sa.Column("browse_last_run_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "browse_rollup" not in columns:
        op.add_column(
            "task_versions",
            sa.Column(
                "browse_rollup",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    _backfill(bind)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("task_versions")
    }
    if "browse_rollup" in columns:
        op.drop_column("task_versions", "browse_rollup")
    if "browse_last_run_at" in columns:
        op.drop_column("task_versions", "browse_last_run_at")
