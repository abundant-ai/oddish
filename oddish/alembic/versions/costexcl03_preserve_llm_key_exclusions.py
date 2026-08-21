"""preserve the legacy LLM-key cost-exclusion policy

Revision ID: costexcl03
Revises: expmodelrename01
Create Date: 2026-08-20 00:00:00.000000

``costexcl02`` originally dropped this table while replacing its policy with
model and experiment exclusions. Its upgrade now preserves the table, while
this idempotent revision repairs databases that already ran the old upgrade.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "costexcl03"
down_revision: Union[str, Sequence[str], None] = "expmodelrename01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_excluded_llm_keys (
            id VARCHAR(64) PRIMARY KEY,
            key_hash VARCHAR(64) NOT NULL,
            key_hint VARCHAR(8) NOT NULL DEFAULT '',
            label VARCHAR(255) NOT NULL DEFAULT '',
            created_by_user_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_excluded_llm_keys_hash_live
        ON cost_excluded_llm_keys (key_hash) WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # The preceding revision preserves this table, so returning to it must too.
    pass
