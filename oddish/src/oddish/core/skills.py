"""Core logic for shared skills.

Validation (`parse_skill`) is pure (no DB) so it can be unit-tested directly.
CRUD (added in a later task) follows the repo's router->core layering.
"""

from __future__ import annotations

import yaml
from fastapi import HTTPException

from oddish.schemas import SkillFile

_FRONTMATTER_DELIM = "---"


def parse_skill(files: list[SkillFile]) -> tuple[str, str]:
    """Validate a skill bundle and return ``(name, description)``.

    Requires a root ``SKILL.md`` with YAML frontmatter providing ``name`` and
    ``description``. Raises ``HTTPException(422)`` on any violation.
    """
    skill_md = next((f for f in files if f.relative_path == "SKILL.md"), None)
    if skill_md is None:
        raise HTTPException(status_code=422, detail="Skill must contain a root SKILL.md")

    text = skill_md.content.lstrip()
    if not text.startswith(_FRONTMATTER_DELIM):
        raise HTTPException(
            status_code=422, detail="SKILL.md must start with YAML frontmatter"
        )
    parts = text.split(_FRONTMATTER_DELIM, 2)
    if len(parts) < 3:
        raise HTTPException(
            status_code=422, detail="SKILL.md frontmatter is not closed with ---"
        )
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="SKILL.md frontmatter is not valid YAML")
    if not isinstance(meta, dict):
        raise HTTPException(status_code=422, detail="SKILL.md frontmatter must be a mapping")

    name = meta.get("name")
    description = meta.get("description")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=422, detail="SKILL.md frontmatter missing 'name'")
    if not description or not isinstance(description, str):
        raise HTTPException(
            status_code=422, detail="SKILL.md frontmatter missing 'description'"
        )
    return name, description
