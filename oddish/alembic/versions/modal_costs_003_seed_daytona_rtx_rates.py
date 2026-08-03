"""seed new daytona gpu rate rows

Revision ID: modal_costs_003
Revises: trajgraph_002
Create Date: 2026-08-03 00:00:00.000000
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.db import generate_id

revision: str = "modal_costs_003"
down_revision: Union[str, Sequence[str], None] = "trajgraph_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED_EFFECTIVE_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)
SEED_NOTE = "daytona.io/pricing 2026-08-03"
SEED_RATES: tuple[tuple[str, str, str], ...] = (
    ("daytona", "gpu:RTX_5090", "0.0003583333"),
    ("daytona", "gpu:RTX_4090", "0.0002750000"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("modal_rates"):
        return
    stmt = sa.text(
        """
        INSERT INTO modal_rates
            (id, provider, sku, usd_per_sec, effective_at, note,
             created_at, updated_at)
        VALUES
            (:id, :provider, :sku, :usd_per_sec, :effective_at, :note,
             NOW(), NOW())
        ON CONFLICT (provider, sku, effective_at) DO NOTHING
        """
    )
    for provider, sku, usd_per_sec in SEED_RATES:
        bind.execute(
            stmt,
            {
                "id": generate_id(),
                "provider": provider,
                "sku": sku,
                "usd_per_sec": Decimal(usd_per_sec),
                "effective_at": SEED_EFFECTIVE_AT,
                "note": SEED_NOTE,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("modal_rates"):
        return
    bind.execute(
        sa.text(
            """
            DELETE FROM modal_rates
            WHERE provider = 'daytona' AND effective_at = :effective_at
              AND sku IN ('gpu:RTX_5090', 'gpu:RTX_4090')
            """
        ),
        {"effective_at": SEED_EFFECTIVE_AT},
    )
