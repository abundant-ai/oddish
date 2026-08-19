"""backfill experiment owners from the exact first trial

Revision ID: expownercreate001
Revises: dropchat001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "expownercreate001"
down_revision: str | Sequence[str] | None = "dropchat001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Include deleted and superseded trial history deliberately. Only the exact
    # first trial can identify an experiment's creator; selecting the earliest
    # later trial with billing data could assign it to an appender. Validate the
    # first trial's payer only after ranking because billed_user_id is not an FK.
    # Deleted users remain eligible: creator provenance survives offboarding.
    op.execute(
        """
        WITH earliest_trial AS MATERIALIZED (
            SELECT DISTINCT ON (t.experiment_id)
                t.experiment_id,
                t.org_id,
                t.billed_user_id
            FROM trials AS t
            WHERE t.experiment_id IS NOT NULL
            ORDER BY t.experiment_id, t.created_at ASC, t.id ASC
        )
        UPDATE experiments AS e
        SET owner_user_id = earliest_trial.billed_user_id
        FROM earliest_trial
        JOIN users AS billed_user
          ON billed_user.id = earliest_trial.billed_user_id
        WHERE e.id = earliest_trial.experiment_id
          AND earliest_trial.org_id = e.org_id
          AND billed_user.org_id = e.org_id
          AND (
              e.owner_user_id IS NULL
              OR e.owner_user_id = '__unattributed__'
          )
        """
    )


def downgrade() -> None:
    # Provenance cannot be safely distinguished from ownership written after
    # this migration, so the data repair is intentionally irreversible.
    pass
