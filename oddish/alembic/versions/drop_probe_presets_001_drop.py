"""drop probe_presets table (rows migrated into skills)

Revision ID: drop_probe_presets_001
Revises: skills_seed_directives_001
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision = "drop_probe_presets_001"
down_revision: Union[str, Sequence[str], None] = "skills_seed_directives_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS probe_presets")


def downgrade() -> None:
    pass
