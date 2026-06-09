"""add documents table

Revision ID: documents_001
Revises: skills_001
Create Date: 2026-06-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "documents_001"
down_revision: Union[str, Sequence[str], None] = "skills_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("digest_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("content_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("s3_key_raw", sa.String(1024), nullable=True),
        sa.Column("raw_mime", sa.String(128), nullable=True),
        sa.Column("raw_filename", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_documents_org_id", "documents", ["org_id"])
    op.create_index(
        "ix_documents_created_by_user_id", "documents", ["created_by_user_id"]
    )
    op.create_index("ix_documents_updated_at", "documents", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_documents_updated_at", table_name="documents")
    op.drop_index("ix_documents_created_by_user_id", table_name="documents")
    op.drop_index("ix_documents_org_id", table_name="documents")
    op.drop_table("documents")
