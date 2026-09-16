"""Merge Thunder fallback with the current staging migration chain."""

from typing import Sequence, Union

revision: str = "merge_thunder_staging_001"
down_revision: Union[str, Sequence[str], None] = (
    "merge_finding_tiers_001",
    "merge_thunder_delivery_001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
