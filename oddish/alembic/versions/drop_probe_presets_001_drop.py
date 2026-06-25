"""drop probe_presets table (rows migrated into skills)

Revision ID: drop_probe_presets_001
Revises: skills_seed_bundles_001
Create Date: 2026-06-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "drop_probe_presets_001"
down_revision: Union[str, Sequence[str], None] = "skills_seed_bundles_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS probe_presets")


def downgrade() -> None:
    pass
