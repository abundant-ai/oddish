"""add provider-wide capacity leases

Revision ID: daytona_capacity_001
Revises: trajgraph_002
Create Date: 2026-08-03 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "daytona_capacity_001"
down_revision: str | Sequence[str] | None = "trajgraph_002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_capacity_leases (
            provider TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            requested_memory_mb INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'WAITING',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            acquired_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            lease_expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (provider, owner_id),
            CONSTRAINT ck_provider_capacity_memory_positive
                CHECK (requested_memory_mb > 0),
            CONSTRAINT ck_provider_capacity_state
                CHECK (state IN ('WAITING', 'HELD'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_provider_capacity_leases_admission
        ON provider_capacity_leases (provider, state, created_at, owner_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_provider_capacity_leases_expiry
        ON provider_capacity_leases (lease_expires_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE provider_capacity_leases")
