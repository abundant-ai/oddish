"""Pure conversion of a probe_presets row into a skills (+ SKILL.md) row.

Kept DB-free so the Alembic data migration and unit tests share one
implementation. ``agent``/``model`` are intentionally dropped — agent/model
are chosen at probe run-time, not stored on the directive.
"""

from __future__ import annotations

from typing import Any

import yaml

from oddish.db import generate_id


def _description_from_prompt(prompt: str) -> str:
    first = (prompt or "").strip().splitlines()[0] if prompt and prompt.strip() else ""
    first = first.strip() or "Migrated probe preset"
    return first[:255]


def preset_row_to_skill(preset: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    description = _description_from_prompt(preset.get("operator_prompt", ""))
    frontmatter = yaml.safe_dump(
        {"name": preset["name"], "description": description},
        sort_keys=False,
    ).strip()
    content = f"---\n{frontmatter}\n---\n\n{preset.get('operator_prompt', '')}"

    skill = {
        "id": preset["id"],
        "org_id": preset.get("org_id"),
        "created_by_user_id": None,
        "name": preset["name"],
        "description": description,
        "is_seed": preset.get("is_seed", False),
        "operator_prompt": preset.get("operator_prompt"),
        "result_focus": preset.get("result_focus"),
        "evaluation_metric": preset.get("evaluation_metric"),
        "created_at": preset.get("created_at"),
        "updated_at": preset.get("updated_at"),
        "deleted_at": preset.get("deleted_at"),
    }
    skill_md = {
        "id": generate_id(),
        "skill_id": preset["id"],
        "relative_path": "SKILL.md",
        "content": content,
    }
    return skill, skill_md
