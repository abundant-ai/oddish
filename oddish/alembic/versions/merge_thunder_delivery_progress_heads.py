"""Merge the delivery progress and Thunder fallback migration branches."""

from typing import Sequence, Union


revision: str = "merge_thunder_delivery_progress_001"
down_revision: Union[str, Sequence[str], None] = (
    "delivery_progress_001",
    "thunder_fallback_001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
