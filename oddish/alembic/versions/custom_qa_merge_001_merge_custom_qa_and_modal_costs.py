"""merge custom-qa and cost-tracking heads

Revision ID: custom_qa_merge_001
Revises: analyzer_runs_001, modal_costs_002
Create Date: 2026-07-22 12:00:00.000000

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "custom_qa_merge_001"
down_revision: Union[str, Sequence[str], None] = (
    "analyzer_runs_001",
    "modal_costs_002",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
