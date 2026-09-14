"""Remember personal UI layouts for each organization membership."""

from alembic import op

revision = "user_ui_layouts_001"
down_revision = "endpoint_health_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE user_ui_layouts (
            user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            layout_key VARCHAR(64) NOT NULL,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, layout_key)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE user_ui_layouts")
