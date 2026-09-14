"""add customers table; deliveries reference it

Every delivery ships to a customer. The free-text ``customer_name``
column becomes a ``customer_id`` foreign key into the new ``customers``
table (one row per org and name).

Revision ID: deliveries_002
Revises: deliveries_001
Create Date: 2026-09-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "deliveries_002"
down_revision: Union[str, Sequence[str], None] = "deliveries_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # On a fresh database, 000_initial's ``Base.metadata.create_all`` has
    # already built the current schema — customers table, customer_id
    # column, no customer_name. Guard each step so both paths converge.
    inspector = sa.inspect(op.get_bind())
    if "customers" not in inspector.get_table_names():
        op.create_table(
            "customers",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("org_id", sa.String(64), nullable=True, index=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "idx_customers_unique_org_name",
            "customers",
            [sa.text("COALESCE(org_id, '')"), "name"],
            unique=True,
        )
    delivery_columns = {c["name"] for c in inspector.get_columns("deliveries")}
    if "customer_id" not in delivery_columns:
        op.add_column(
            "deliveries",
            sa.Column(
                "customer_id",
                sa.String(64),
                sa.ForeignKey("customers.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
    if "customer_name" in delivery_columns:
        op.drop_column("deliveries", "customer_name")


def downgrade() -> None:
    op.add_column(
        "deliveries",
        sa.Column("customer_name", sa.String(255), nullable=True),
    )
    op.drop_column("deliveries", "customer_id")
    op.drop_table("customers")
