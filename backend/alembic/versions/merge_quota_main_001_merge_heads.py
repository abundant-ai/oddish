"""merge quotas + drop_user_run_probe_default heads

Revision ID: merge_quota_main_001
Revises: add_quotas_table_001, t6u7v8w9x0y1
Create Date: 2026-07-01 00:00:00.000000

Empty merge migration reconciling the two alembic heads created by merging
origin/main (``t6u7v8w9x0y1``, drop ``users.run_probe_default``) into the User
Quotas MVP stack (``add_quotas_table_001``). No schema change -- it only rejoins
the divergent history into a single head so the migration-head guard passes.
"""

from typing import Sequence, Union

revision: str = "merge_quota_main_001"
down_revision: Union[str, Sequence[str], None] = (
    "add_quotas_table_001",
    "t6u7v8w9x0y1",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
