"""merge tag and run-probe heads

Revision ID: merge_tag_run_probe_heads
Revises: merge_tag_mine_filter_heads, run_probe_001
Create Date: 2026-06-11 21:46:43.083629

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "merge_tag_run_probe_heads"
down_revision: Union[str, Sequence[str], None] = (
    "merge_tag_mine_filter_heads",
    "run_probe_001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
