"""seed vendored skillz + harbor-lh bundle skills

Revision ID: skills_seed_bundles_001
Revises: skills_seed_directives_001
Create Date: 2026-06-25 00:00:00.000000
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from oddish.db import generate_id
from oddish.seeds.loader import load_seed_bundles

revision = "skills_seed_bundles_001"
down_revision: Union[str, Sequence[str], None] = "skills_seed_directives_001"
branch_labels = None
depends_on = None

_TS = datetime(2026, 6, 25, tzinfo=timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    taken = {
        r[0]
        for r in bind.execute(
            sa.text("SELECT name FROM skills WHERE org_id IS NULL AND deleted_at IS NULL")
        ).all()
    }
    for b in load_seed_bundles():
        if b["name"] in taken:
            continue
        skill_id = generate_id()
        bind.execute(
            sa.text(
                "INSERT INTO skills (id, org_id, created_by_user_id, name, "
                "description, is_seed, operator_prompt, result_focus, "
                "evaluation_metric, created_at, updated_at, deleted_at) VALUES "
                "(:id, NULL, NULL, :name, :description, true, NULL, NULL, NULL, "
                ":ts, :ts, NULL)"
            ),
            {"id": skill_id, "name": b["name"], "description": b["description"][:255] or b["name"], "ts": _TS},
        )
        for rel, content in b["files"]:
            bind.execute(
                sa.text(
                    "INSERT INTO skill_files (id, skill_id, relative_path, content) "
                    "VALUES (:id, :skill_id, :rel, :content)"
                ),
                {"id": generate_id(), "skill_id": skill_id, "rel": rel, "content": content},
            )
        taken.add(b["name"])


def downgrade() -> None:
    pass
