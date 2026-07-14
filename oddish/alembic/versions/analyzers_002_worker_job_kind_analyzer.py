"""add ANALYZER value to worker_job_kind enum

``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block, so each
statement runs in an autocommit block. Enum values cannot be dropped, so the
downgrade is a documented no-op.
"""

from alembic import op

revision = "analyzers_002"
down_revision = "analyzers_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'ANALYZER'")


def downgrade() -> None:
    # Postgres cannot remove a value from an enum type; no-op by design.
    pass
