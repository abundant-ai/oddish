"""merge prompt registry and trajectory metrics heads

Revision ID: prompts_trajectory_merge_001
Revises: prompts_merge_001, trial_trajectory_metrics_001
"""

from typing import Sequence, Union


revision: str = "prompts_trajectory_merge_001"
down_revision: Union[str, Sequence[str], None] = (
    "prompts_merge_001",
    "trial_trajectory_metrics_001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
