"""add experiments.owner and experiments.link (per-experiment provenance)

Stores the creating run's submitter (``owner``, a display string) and PR/run
URL (``link``) on the experiment itself, so the experiment always shows the
PR/owner it was created for -- immune to other runs overwriting the shared,
mutable ``task.link`` / ``task.tags``.

Backfill (one-time): seed each experiment from its EARLIEST linked task (the
"creator"), matching the set-once stamping the write path now does going
forward. ``owner`` prefers the task's github username, falling back to its user;
``link`` comes from the task's link column.

Revision ID: exp_owner_link_001
Revises: drop_probe_presets_001
Create Date: 2026-06-19 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "exp_owner_link_001"
down_revision: Union[str, Sequence[str], None] = "drop_probe_presets_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS owner TEXT")
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS link TEXT")
    op.execute(
        """
        UPDATE experiments e
        SET owner = sub.owner,
            link  = sub.link
        FROM (
            SELECT DISTINCT ON (te.experiment_id)
                   te.experiment_id AS experiment_id,
                   COALESCE(t.tags ->> 'github_username', t.user) AS owner,
                   t.link AS link
            FROM   task_experiments te
            JOIN   tasks t ON t.id = te.task_id
            WHERE  te.deleted_at IS NULL
              AND  t.deleted_at IS NULL
            ORDER BY te.experiment_id, t.created_at ASC, t.id ASC
        ) AS sub
        WHERE e.id = sub.experiment_id
          AND e.owner IS NULL
          AND e.link IS NULL
          AND e.deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS owner")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS link")
