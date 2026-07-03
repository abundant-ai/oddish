"""merge billed_user_id + experiment_trials heads

Revision ID: merge_billed_user_main_001
Revises: billed_user_001, exp_trials_join_001
Create Date: 2026-07-01 00:00:00.000000

Empty merge migration reconciling the two alembic heads created by merging
origin/main (``exp_trials_join_001``, add experiment_trials) into the User
Quotas MVP stack (``billed_user_001``, add trials.billed_user_id). No schema
change -- it only rejoins the divergent history into a single head so the
migration-head guard passes.
"""

from typing import Sequence, Union

revision: str = "merge_billed_user_main_001"
down_revision: Union[str, Sequence[str], None] = (
    "billed_user_001",
    "exp_trials_join_001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
