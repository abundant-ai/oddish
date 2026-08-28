"""remove inherited learnability model renames

Revision ID: expmodelrename02
Revises: costexcl03
Create Date: 2026-08-21 00:00:00.000000

"""

from alembic import op

revision = "expmodelrename02"
down_revision = "costexcl03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE experiments
        SET public_model_renames = public_model_renames - ARRAY(
            SELECT key
            FROM jsonb_each_text(public_model_renames)
            WHERE key ILIKE '%learnability%' OR value ILIKE '%learnability%'
        )
        WHERE public_model_renames::text ILIKE '%learnability%'
        """
    )


def downgrade() -> None:
    pass
