"""Add task_versions.network_mode, the real Harbor field allow_internet
was (silently, always) reading None for.

Revision ID: taskreg_005
Revises: taskreg_004
"""

from __future__ import annotations

from alembic import op

revision = "taskreg_005"
down_revision = "taskreg_004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # AccessExclusiveLock is only held briefly (nullable column, no default
    # -- no table rewrite), but a long-running reader on task_versions could
    # otherwise queue every writer behind this DDL. Fail fast instead. LOCAL
    # so it resets at this migration's transaction commit rather than
    # leaking (session-scoped) onto every later migration sharing this
    # connection.
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute(
        "ALTER TABLE task_versions ADD COLUMN IF NOT EXISTS network_mode TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_versions DROP COLUMN IF EXISTS network_mode")
