"""add model_display_names

Revision ID: modeldisp01
Revises: verdict_state_001
Create Date: 2026-08-04 00:00:00.000000

Operator-managed aliases from a stored model id to the name rendered on
published share pages. Display-only: nothing here feeds cost accounting or
queue routing, both of which keep reading ``trials.model``. ``deleted_at`` is
the soft-delete tombstone; the partial UNIQUE keeps one live row per model so
a removed alias can be re-added.

"""

from typing import Sequence, Union

from alembic import op

revision: str = "modeldisp01"
down_revision: Union[str, Sequence[str], None] = "verdict_state_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_display_names (
            id VARCHAR(64) PRIMARY KEY,
            model_name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255) NOT NULL,
            created_by_user_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_display_names_model_live
        ON model_display_names (model_name) WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model_display_names")
