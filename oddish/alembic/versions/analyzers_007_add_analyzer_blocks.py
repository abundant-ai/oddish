"""add analyzer_blocks table

Standalone table for the composable AnalyzerBlock primitive. Reuses the existing
``jobstatus`` enum (create_type=False -- do not CREATE TYPE). ``000_initial_schema``
runs ``create_all()``, so on a fresh DB the table already exists before this
migration runs -- hence the inspector guard.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "analyzers_007"
down_revision: Union[str, Sequence[str], None] = "analyzers_006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "analyzer_blocks" in sa.inspect(bind).get_table_names():
        return
    jobstatus = postgresql.ENUM(name="jobstatus", create_type=False)
    op.create_table(
        "analyzer_blocks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analyzer_id", sa.String(64), nullable=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.Text, nullable=False),
        sa.Column("llm_client_type", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text, nullable=True),
        sa.Column("input", JSONB, nullable=True),
        sa.Column("output", JSONB, nullable=True),
        sa.Column("status", jobstatus, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("job_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_duration_seconds", sa.Float, nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
    )
    op.create_index(
        "idx_analyzer_blocks_analyzer_id_live",
        "analyzer_blocks",
        ["analyzer_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_analyzer_blocks_analyzer_id", "analyzer_blocks", ["analyzer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_analyzer_blocks_analyzer_id", table_name="analyzer_blocks")
    op.drop_index("idx_analyzer_blocks_analyzer_id_live", table_name="analyzer_blocks")
    op.drop_table("analyzer_blocks")
