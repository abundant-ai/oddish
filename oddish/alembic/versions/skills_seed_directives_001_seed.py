"""seed built-in directive skills (cheat-detector, verifier-critic, ...)

Revision ID: skills_seed_directives_001
Revises: skills_from_presets_001
Create Date: 2026-06-25 00:00:00.000000
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.alembic_seed_directives import SEED_DIRECTIVE_SKILLS
from oddish.db import generate_id

revision = "skills_seed_directives_001"
down_revision: Union[str, Sequence[str], None] = "skills_from_presets_001"
branch_labels = None
depends_on = None

_TS = datetime(2026, 6, 25, tzinfo=timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        r[0]
        for r in bind.execute(sa.text("SELECT id FROM skills")).all()
    }
    for s in SEED_DIRECTIVE_SKILLS:
        if s["id"] in existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO skills (id, org_id, created_by_user_id, name, "
                "description, is_seed, operator_prompt, result_focus, "
                "evaluation_metric, created_at, updated_at, deleted_at) VALUES "
                "(:id, NULL, NULL, :name, :description, true, :operator_prompt, "
                ":result_focus, :evaluation_metric, :ts, :ts, NULL) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "operator_prompt": s["operator_prompt"],
                "result_focus": s["result_focus"],
                "evaluation_metric": s["evaluation_metric"],
                "ts": _TS,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO skill_files (id, skill_id, relative_path, content) "
                "VALUES (:id, :skill_id, 'SKILL.md', :content)"
            ),
            {"id": generate_id(), "skill_id": s["id"], "content": s["skill_md"]},
        )


def downgrade() -> None:
    bind = op.get_bind()
    ids = tuple(s["id"] for s in SEED_DIRECTIVE_SKILLS)
    bind.execute(sa.text("DELETE FROM skill_files WHERE skill_id IN :ids").bindparams(
        sa.bindparam("ids", expanding=True)), {"ids": list(ids)})
    bind.execute(sa.text("DELETE FROM skills WHERE id IN :ids").bindparams(
        sa.bindparam("ids", expanding=True)), {"ids": list(ids)})
