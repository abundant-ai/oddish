"""Reserve queue slots before launch and persist fair-share scheduling cursors."""

from alembic import op

revision = "qa_dispatch_001"
down_revision = "deliveries_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE queue_slots ADD COLUMN IF NOT EXISTS launch_demand jsonb")
    op.execute(
        "CREATE TABLE IF NOT EXISTS queue_dispatch_state (id integer PRIMARY KEY, cursors jsonb NOT NULL DEFAULT '{}'::jsonb)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE queue_dispatch_state")
    op.execute("ALTER TABLE queue_slots DROP COLUMN launch_demand")
