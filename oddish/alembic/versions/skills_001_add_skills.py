"""add skills + skill_files tables

Revision ID: skills_001
Revises: trial_is_probe_001
Create Date: 2026-06-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "skills_001"
down_revision: Union[str, Sequence[str], None] = "trial_is_probe_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_seed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_skills_org_id", "skills", ["org_id"])
    op.create_index("ix_skills_created_by_user_id", "skills", ["created_by_user_id"])
    op.create_index(
        "idx_skills_unique_org_name",
        "skills",
        [sa.text("COALESCE(org_id, '')"), "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "skill_files",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "skill_id",
            sa.String(64),
            sa.ForeignKey("skills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_index("ix_skill_files_skill_id", "skill_files", ["skill_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_files_skill_id", table_name="skill_files")
    op.drop_table("skill_files")
    op.drop_index("idx_skills_unique_org_name", table_name="skills")
    op.drop_index("ix_skills_created_by_user_id", table_name="skills")
    op.drop_index("ix_skills_org_id", table_name="skills")
    op.drop_table("skills")
