"""add trials.llm_key_hash + cost_excluded_llm_keys

Revision ID: costexclkeys01
Revises: modal_costs_001
Create Date: 2026-07-17 00:00:00.000000

Supports excluding sponsored/free LLM-provider-key spend from cost accounting.

* ``trials.llm_key_hash`` -- nullable ``VARCHAR(64)``, the SHA-256 of the
  platform provider API key a finished trial ran on. Stamped forward-only at
  settlement; pre-rollout rows stay NULL and are never excluded. No index: the
  exclusion probe correlates on ``cost_excluded_llm_keys.key_hash`` (its own
  unique index), and cost queries already scan trials by org/finished_at.
* ``cost_excluded_llm_keys`` -- the admin-managed list. Only a one-way
  ``key_hash`` (equality match is all the feature needs) plus a masked
  ``key_hint`` for display; the plaintext key is never stored. ``deleted_at`` is
  the soft-delete tombstone; the partial UNIQUE keeps one live row per hash so a
  removed key can be re-added.

"""

from typing import Sequence, Union

from alembic import op

revision: str = "costexclkeys01"
down_revision: Union[str, Sequence[str], None] = "modal_costs_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS llm_key_hash VARCHAR(64)")
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
    op.execute("DROP TABLE IF EXISTS cost_excluded_llm_keys")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS llm_key_hash")
