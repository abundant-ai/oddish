"""widen trials.idempotency_key

The import path builds ``trials.idempotency_key`` as
``import:{source_trial_id}:{experiment_id}`` and the combine path as
``combine:{experiment_id}:{source_trial_id}``. Both compose the key from
task-derived ids that were already widened in ``long_task_ids_001`` (task ids to
128 chars, trial ids to 160), but the derived ``idempotency_key`` column was
left at the original ``VARCHAR(64)``. A long Harbor task name therefore produced
a key longer than 64 chars; Postgres rejects (does not truncate) the over-length
value, so ``POST /trials/import/init`` 500'd deterministically for long-named
tasks. Widen the column to match ``TRIAL_IDEMPOTENCY_KEY_MAX_LENGTH`` so every
realistic composed key fits and keeps its idempotency guarantee.

Increasing a ``varchar`` length limit is a catalog-only change in Postgres: it
does not rewrite the table or rebuild the existing unique index
(``idx_trials_idempotency_key``).

Revision ID: widen_idem_key_001
Revises: exp_owner_link_001
Create Date: 2026-06-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "widen_idem_key_001"
down_revision: Union[str, Sequence[str], None] = "exp_owner_link_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Widen ``trials.idempotency_key`` from VARCHAR(64) to VARCHAR(255)."""
    op.execute("ALTER TABLE trials ALTER COLUMN idempotency_key TYPE VARCHAR(255)")


def downgrade() -> None:
    """Restore the original VARCHAR(64).

    Safe only while no row holds a key longer than 64 chars; such rows could
    not have existed before this migration (the insert that would create them
    was exactly the failure this migration fixes).
    """
    op.execute("ALTER TABLE trials ALTER COLUMN idempotency_key TYPE VARCHAR(64)")
