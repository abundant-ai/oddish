"""add analyzers.save_trial_analyses flag

Additive column: NOT NULL with server default 'false' so the backfill is
instant and existing rows read false. Idempotent + autocommit, mirroring
analyzers_001.
"""

from alembic import op

revision = "analyzers_003"
down_revision = "analyzers_002"
branch_labels = None
depends_on = None


def _autocommit(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sql)


def upgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit(
        "ALTER TABLE analyzers "
        "ADD COLUMN IF NOT EXISTS save_trial_analyses BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit("ALTER TABLE analyzers DROP COLUMN IF EXISTS save_trial_analyses")
