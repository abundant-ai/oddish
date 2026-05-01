"""add_probe_presets

Replaces the flat-file probe-presets.json (which was read/written from
the frontend container's filesystem and didn't survive deploys) with a
proper org-scoped table.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-05-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "probe_presets",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("operator_prompt", sa.Text, nullable=False),
        sa.Column("result_focus", sa.Text, nullable=True),
        sa.Column("evaluation_metric", sa.String(32), nullable=True),
        sa.Column("ratio_unit", sa.String(64), nullable=True),
        sa.Column("ratio_verb", sa.String(64), nullable=True),
        sa.Column("include_prior_attempts", JSONB, nullable=True),
        sa.Column("is_seed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("idx_probe_presets_org_id", "probe_presets", ["org_id"])


def downgrade() -> None:
    op.drop_index("idx_probe_presets_org_id", table_name="probe_presets")
    op.drop_table("probe_presets")
