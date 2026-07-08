"""add trials.cost_is_estimated

Revision ID: cost_est_001
Revises: org_quota_idx_001
Create Date: 2026-07-08 00:00:00.000000

``settle_cost_usd`` now writes token-derived estimates into ``cost_usd`` when
the harness reports $0 for a model it cannot price, so "cost_usd IS NOT NULL"
no longer implies the value came from the runtime. This flag records the
provenance so API responses can keep the documented ``cost_is_estimated``
contract.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "cost_est_001"
down_revision: Union[str, Sequence[str], None] = "org_quota_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trials", sa.Column("cost_is_estimated", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("trials", "cost_is_estimated")
