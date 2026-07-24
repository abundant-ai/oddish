"""Add experiments.api_key_id (audit-only submitter provenance).

Not backfilled: dashboard-created experiments have no key, so NULL is legitimate.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "expapikey01"
down_revision: Union[str, Sequence[str], None] = "prod_schema_repair_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("experiments")}
    if "api_key_id" not in columns:
        op.add_column(
            "experiments",
            sa.Column("api_key_id", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("experiments")}
    if "api_key_id" in columns:
        op.drop_column("experiments", "api_key_id")
