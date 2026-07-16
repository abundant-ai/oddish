"""add llm_usage

Per-call usage/cost rows for internal (non-harbor-trial) LLM calls, written by
the ``oddish.core.llm`` funnel and grouped by ``handler`` on /admin/costs.

``000_initial_schema`` runs ``create_all()``, so on a fresh DB the table
already exists before this migration runs -- hence ``if_not_exists=True``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "llm_usage_001"
down_revision: Union[str, Sequence[str], None] = "analyzers_006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("handler", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("input_tokens", sa.BigInteger, nullable=True),
        sa.Column("output_tokens", sa.BigInteger, nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger, nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger, nullable=True),
        sa.Column("cost_usd", sa.Float, nullable=True),
        sa.Column(
            "cost_basis",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'api'"),
        ),
        sa.Column("trial_id", sa.String(160), nullable=True),
        sa.Column("task_id", sa.String(160), nullable=True),
        sa.Column("experiment_id", sa.String(64), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column(
            "success", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column("error", sa.Text, nullable=True),
        if_not_exists=True,
    )
    op.create_index(
        "ix_llm_usage_created_at", "llm_usage", ["created_at"], if_not_exists=True
    )
    op.create_index(
        "ix_llm_usage_handler_created_at",
        "llm_usage",
        ["handler", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("llm_usage")
