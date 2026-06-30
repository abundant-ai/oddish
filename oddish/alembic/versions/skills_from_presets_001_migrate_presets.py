"""migrate probe_presets rows into skills (+ SKILL.md), de-duplicating names

Revision ID: skills_from_presets_001
Revises: skills_directive_001
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.core.probe.preset_migration import preset_row_to_skill

revision: str = "skills_from_presets_001"
down_revision: Union[str, Sequence[str], None] = "skills_directive_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("probe_presets"):
        return  # fresh DB without the legacy table

    presets = bind.execute(
        sa.text(
            "SELECT id, org_id, name, operator_prompt, result_focus, "
            "evaluation_metric, is_seed, created_at, updated_at, deleted_at "
            "FROM probe_presets WHERE deleted_at IS NULL"
        )
    ).mappings().all()

    # Names already taken in skills (for the partial unique (org_id, name) index).
    taken = {
        (r["org_id"], r["name"])
        for r in bind.execute(
            sa.text("SELECT org_id, name FROM skills WHERE deleted_at IS NULL")
        ).mappings().all()
    }

    for preset in presets:
        skill, skill_md = preset_row_to_skill(dict(preset))
        key = (skill["org_id"], skill["name"])
        if key in taken:
            skill["name"] = f"{skill['name']} (preset)"
        taken.add((skill["org_id"], skill["name"]))

        bind.execute(
            sa.text(
                "INSERT INTO skills (id, org_id, created_by_user_id, name, "
                "description, is_seed, operator_prompt, result_focus, "
                "evaluation_metric, created_at, updated_at, deleted_at) VALUES "
                "(:id, :org_id, :created_by_user_id, :name, :description, "
                ":is_seed, :operator_prompt, :result_focus, :evaluation_metric, "
                ":created_at, :updated_at, :deleted_at) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            skill,
        )
        bind.execute(
            sa.text(
                "INSERT INTO skill_files (id, skill_id, relative_path, content) "
                "VALUES (:id, :skill_id, :relative_path, :content)"
            ),
            skill_md,
        )


def downgrade() -> None:
    # Non-reversible data migration; presets are re-derivable only from backup.
    pass
