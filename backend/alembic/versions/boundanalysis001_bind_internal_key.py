"""bind internal API keys to their requesting analysis trial

Revision ID: boundanalysis001
Revises: expownercreate001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "boundanalysis001"
down_revision: str | Sequence[str] | None = "expownercreate001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
        "bound_analysis_trial_id VARCHAR(64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_keys_bound_analysis_trial_id "
        "ON api_keys (bound_analysis_trial_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_api_keys_bound_analysis_trial_id")
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS bound_analysis_trial_id")
