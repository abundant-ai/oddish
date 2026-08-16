"""replace llm-key cost exclusion with model and experiment exclusions

Revision ID: costexcl02
Revises: agentcap01
Create Date: 2026-08-15 00:00:00.000000

Admin-managed lists of spend that was never really paid for. Replaces the
key-hash list (``cost_excluded_llm_keys``, dropped here) with the two axes
operators actually reach for: a **model** that costs nothing (sponsored
capacity, free preview tiers) and an **experiment** that was comped.

Both correlate on columns ``trials`` already indexes -- ``trials.model`` and
``trials.experiment_id`` -- so neither needs a new index on that hot table.
``deleted_at`` is the soft-delete tombstone; the partial UNIQUEs keep one live
row per model / per experiment so a removed entry can be re-added.

``trials.llm_key_hash`` is deliberately NOT dropped here. Nothing reads it any
more, so it is inert -- but both deploy workflows run migrations *before* the
code deploy (``AGENTS.md``: "the backend can hard-require new schema on its hot
paths"). That ordering is the expand pattern: safe for ADD, wrong for DROP.
Dropping the column here would leave the still-serving previous release mapping
a column that no longer exists, failing every ``select(TrialModel)`` for the
length of the image build. The drop belongs in a follow-up migration, once a
release that never touches the column is fully rolled out.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "costexcl02"
down_revision: Union[str, Sequence[str], None] = "agentcap01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_excluded_models (
            id VARCHAR(64) PRIMARY KEY,
            model_name VARCHAR(255) NOT NULL,
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_excluded_models_model_live
        ON cost_excluded_models (model_name) WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_excluded_experiments (
            id VARCHAR(64) PRIMARY KEY,
            experiment_id VARCHAR(64) NOT NULL,
            experiment_name VARCHAR(255) NOT NULL DEFAULT '',
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_excluded_experiments_exp_live
        ON cost_excluded_experiments (experiment_id) WHERE deleted_at IS NULL
        """
    )
    # Dropped last, so a failed create above leaves nothing half-done.
    op.execute("DROP TABLE IF EXISTS cost_excluded_llm_keys")


def downgrade() -> None:
    # Recreates the key list's shape only; its rows are not recoverable,
    # nothing else stored the hashes.
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
    op.execute("DROP TABLE IF EXISTS cost_excluded_experiments")
    op.execute("DROP TABLE IF EXISTS cost_excluded_models")
