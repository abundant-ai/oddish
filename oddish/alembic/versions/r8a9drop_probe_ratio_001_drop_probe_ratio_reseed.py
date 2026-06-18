"""drop probe ratio columns + reseed cheat presets to JSON result_focus.

Drops ratio_unit and ratio_verb from probe_presets (the ratio evaluation
metric is removed). Reseeds the two ratio cheat presets to carry a JSON
Schema in result_focus instead.

Revision ID: r8a9drop_probe_ratio_001
Revises: long_task_ids_001
Create Date: 2026-06-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r8a9drop_probe_ratio_001"
down_revision: Union[str, Sequence[str], None] = "long_task_ids_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHEAT_SCHEMA = (
    '{"type":"object","properties":'
    '{"cheats":{"type":"array","items":{"type":"object",'
    '"properties":{"layer":{"type":"string"},"mechanism":{"type":"string"},'
    '"succeeded":{"type":"boolean"}},"required":["layer","mechanism","succeeded"]}}},'
    '"required":["cheats"]}'
)


def upgrade() -> None:
    op.execute("ALTER TABLE probe_presets DROP COLUMN IF EXISTS ratio_unit")
    op.execute("ALTER TABLE probe_presets DROP COLUMN IF EXISTS ratio_verb")
    # Reseed the ratio cheat presets to carry a JSON Schema instead.
    op.execute(
        "UPDATE probe_presets SET evaluation_metric = NULL, "
        f"result_focus = '{_CHEAT_SCHEMA}' "
        "WHERE is_seed = true AND evaluation_metric = 'ratio'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE probe_presets ADD COLUMN IF NOT EXISTS ratio_unit varchar(30)"
    )
    op.execute(
        "ALTER TABLE probe_presets ADD COLUMN IF NOT EXISTS ratio_verb varchar(30)"
    )
