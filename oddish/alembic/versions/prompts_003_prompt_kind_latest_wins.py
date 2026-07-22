"""prompts: key->kind (enum vocabulary), drop active_version (latest wins)

Transforms DBs that ran the original prompts_001 shape. Fresh DBs get the
new shape from create_all()/amended prompts_001, so every step is guarded.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "prompts_003"
down_revision: Union[str, Sequence[str], None] = "prompts_merge_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("prompts")}
    if "key" in cols:
        op.alter_column("prompts", "key", new_column_name="kind")
        op.execute("UPDATE prompts SET kind = 'QA_PRE_TRIAL' WHERE kind = 'pre_trial_qa'")
        op.execute("UPDATE prompts SET kind = 'QA_POST_TRIAL' WHERE kind = 'post_trial_qa'")
    if "active_version" in cols:
        op.drop_column("prompts", "active_version")
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("prompts")}
    if "idx_prompts_unique_key" in indexes:
        op.execute("ALTER INDEX idx_prompts_unique_key RENAME TO idx_prompts_unique_kind")


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("prompts")}
    if "idx_prompts_unique_kind" in indexes:
        op.execute("ALTER INDEX idx_prompts_unique_kind RENAME TO idx_prompts_unique_key")
    cols = {c["name"] for c in sa.inspect(bind).get_columns("prompts")}
    if "active_version" not in cols:
        op.add_column("prompts", sa.Column("active_version", sa.Integer, nullable=True))
    if "kind" in cols:
        op.execute("UPDATE prompts SET kind = 'pre_trial_qa' WHERE kind = 'QA_PRE_TRIAL'")
        op.execute("UPDATE prompts SET kind = 'post_trial_qa' WHERE kind = 'QA_POST_TRIAL'")
        op.alter_column("prompts", "kind", new_column_name="key")
