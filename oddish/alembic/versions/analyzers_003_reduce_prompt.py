"""add analyzers.reduce_prompt

Persist the reduce-stage prompt that produced an analyzer's narrative sections.
Additive nullable column; the ``IF NOT EXISTS`` guard keeps it fresh-DB
idempotent, and it runs in its own autocommit block so a re-triggered run is
safe and no single txn holds a lock while requesting another (matches the
``analyzers_001`` convention).
"""

from alembic import op

revision = "analyzers_003"
down_revision = "analyzers_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE analyzers ADD COLUMN IF NOT EXISTS reduce_prompt TEXT")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE analyzers DROP COLUMN IF EXISTS reduce_prompt")
