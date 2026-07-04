"""add user_provider_keys

Per-user BYOK provider API keys: AES-GCM ciphertext only, one live row per
(user, vendor). Plain-text status/vendor + CHECK constraints (not PG enums)
so the migration downgrades cleanly.

Revision ID: u7v8w9x0y1z2
Revises: apk_role_backend_001
Create Date: 2026-07-04 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "u7v8w9x0y1z2"
down_revision: Union[str, Sequence[str], None] = "apk_role_backend_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_provider_keys (
            id VARCHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            org_id VARCHAR(64),
            vendor VARCHAR(16) NOT NULL,
            ciphertext BYTEA NOT NULL,
            key_version INTEGER NOT NULL DEFAULT 1,
            key_hint VARCHAR(8) NOT NULL DEFAULT '',
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            last_validated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT ck_user_provider_keys_vendor
                CHECK (vendor IN ('anthropic', 'openai')),
            CONSTRAINT ck_user_provider_keys_status
                CHECK (status IN ('active', 'invalid'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_provider_keys_unique_live
        ON user_provider_keys (user_id, vendor) WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_user_provider_keys_user_id
        ON user_provider_keys (user_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_provider_keys")
