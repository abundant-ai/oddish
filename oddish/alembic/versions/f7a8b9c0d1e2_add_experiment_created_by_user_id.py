"""add experiment created_by_user_id

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-05-01 02:00:00.000000

Adds an explicit `experiments.created_by_user_id` column so the displayed
author is the principal who kicked off the experiment, instead of being
derived from whichever task happens to be linked.

Backfills two things on upgrade:

  1. ``experiments.created_by_user_id`` is set from the *earliest* linked
     task's ``created_by_user_id`` (the closest proxy we have for the
     original creator on historical data).

  2. ``tasks.user`` rows that contain the OS-username sentinels left over
     from before the author-from-auth fix (``root``, ``ubuntu``,
     ``unknown``) are rewritten to the linked Clerk user's email when we
     have that mapping (``tasks.created_by_user_id`` -> ``users.email``).
     Rows whose ``user`` is anything else (including legitimate display
     names set via ``--user``) are left untouched.

Both backfills are best-effort: rows without a recoverable
``created_by_user_id`` simply stay ``NULL`` / unchanged. The dashboard
falls back to its existing latest-task derivation in that case.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column("created_by_user_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "idx_experiments_created_by_user_id",
        "experiments",
        ["created_by_user_id"],
        unique=False,
        if_not_exists=True,
    )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    has_users = inspector.has_table("users")
    has_task_experiments = inspector.has_table("task_experiments")

    if has_task_experiments:
        # Backfill experiments.created_by_user_id from the earliest linked
        # task's created_by_user_id. Quoting `user` (reserved in postgres).
        op.execute(
            """
            UPDATE experiments e
            SET created_by_user_id = sub.created_by_user_id
            FROM (
                SELECT DISTINCT ON (te.experiment_id)
                    te.experiment_id,
                    t.created_by_user_id
                FROM task_experiments te
                JOIN tasks t ON t.id = te.task_id
                WHERE t.created_by_user_id IS NOT NULL
                ORDER BY te.experiment_id, t.created_at ASC, t.id ASC
            ) AS sub
            WHERE e.id = sub.experiment_id
              AND e.created_by_user_id IS NULL
            """
        )

    if has_users:
        # Rewrite OS-username sentinels on historical task rows so the
        # detail view (which still derives author from the earliest task's
        # `user` string) shows a real identity. Only touches rows where the
        # FK to users is set and the current value is one of the known
        # sentinels — anything user-supplied via --user is preserved.
        op.execute(
            """
            UPDATE tasks t
            SET "user" = u.email
            FROM users u
            WHERE t.created_by_user_id = u.id
              AND u.email IS NOT NULL
              AND u.email <> ''
              AND t."user" IN ('root', 'ubuntu', 'unknown')
            """
        )


def downgrade() -> None:
    op.drop_index(
        "idx_experiments_created_by_user_id",
        table_name="experiments",
        if_exists=True,
    )
    op.drop_column("experiments", "created_by_user_id")
