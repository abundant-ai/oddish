"""seed daytona compute rate rows

Revision ID: modal_costs_002
Revises: modal_costs_001
Create Date: 2026-07-22 00:00:00.000000

Adds daytona rows to the ``modal_rates`` card so daytona sandbox spans price
instead of recording as ``no_rate``. Only the sandbox class exists for daytona
(the oddish worker always runs on Modal); GPU trials route to Modal, so the
daytona GPU rows are defensive.

Public list prices from daytona.io/pricing, converted per-hour / 3600 to
per-second. Free-tier allowances (20 vCPU-h + 40 GiB-h/day, $200 credit) are
not modeled -- this is a gross list-price estimate, matching the Modal rows.

Idempotent via ON CONFLICT DO NOTHING on the (provider, sku, effective_at)
natural key, so the create_all-bootstrapped path and re-runs stay safe.
Mirrored by the daytona block of oddish.costs.modal_cost.DEFAULT_RATES; the
drift between the union of all modal_costs_* seeds and DEFAULT_RATES is
unit-tested (tests/test_modal_cost_pricing.py).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.db import generate_id

revision: str = "modal_costs_002"
down_revision: Union[str, Sequence[str], None] = "modal_costs_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED_EFFECTIVE_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)
SEED_NOTE = "daytona.io/pricing 2026-07-22"
SEED_RATES: tuple[tuple[str, str, str], ...] = (
    ("daytona", "sandbox:cpu_core_sec", "0.0000140"),
    ("daytona", "sandbox:mem_gib_sec", "0.0000045"),
    ("daytona", "gpu:H200", "0.0012611111"),
    ("daytona", "gpu:H100", "0.0010972222"),
    ("daytona", "gpu:RTX_PRO_6000", "0.0008416667"),
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
            """
        ),
        {"effective_at": SEED_EFFECTIVE_AT},
    )
