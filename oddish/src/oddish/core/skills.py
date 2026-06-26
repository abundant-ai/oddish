"""Core logic for shared skills.

Validation (`parse_skill`) is pure (no DB) so it can be unit-tested directly.
CRUD follows the repo's router->core layering: functions receive an AsyncSession and never commit — the calling router owns the transaction.
"""

from __future__ import annotations

import re
import yaml
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import SkillFileModel, SkillModel, utcnow
from oddish.schemas import SkillCreate, SkillFile, SkillUpdate
from oddish.core.result_focus_schema import (
    UnsupportedSchemaError,
    normalize_findings_schema,
    parse_result_focus,
)

_FRONTMATTER_DELIM = "---"


def parse_skill(files: list[SkillFile]) -> tuple[str, str]:
    """Validate a skill bundle and return ``(name, description)``.

    Requires a root ``SKILL.md`` with YAML frontmatter providing ``name`` and
    ``description``. Raises ``HTTPException(422)`` on any violation.
    """
    skill_md = next((f for f in files if f.relative_path == "SKILL.md"), None)
    if skill_md is None:
        raise HTTPException(
            status_code=422, detail="Skill must contain a root SKILL.md"
        )

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
        raise HTTPException(
            status_code=422, detail="SKILL.md frontmatter is not valid YAML"
        )
    if not isinstance(meta, dict):
        raise HTTPException(
            status_code=422, detail="SKILL.md frontmatter must be a mapping"
        )

    name = meta.get("name")
    description = meta.get("description")
    if not name or not isinstance(name, str):
        raise HTTPException(
            status_code=422, detail="SKILL.md frontmatter missing 'name'"
        )
    if not description or not isinstance(description, str):
        raise HTTPException(
            status_code=422, detail="SKILL.md frontmatter missing 'description'"
        )
    return name, description


def _rewrite_skill_name(files: list[SkillFile], new_name: str) -> list[SkillFile]:
    """Return ``files`` with the root SKILL.md frontmatter ``name:`` set to
    ``new_name`` (used when a collision forces a version-suffixed name).

    Targeted line edit, not a YAML re-dump, so the rest of the file's
    formatting is preserved. Assumes ``files`` already passed ``parse_skill``.
    """
    out: list[SkillFile] = []
    for f in files:
        if f.relative_path != "SKILL.md":
            out.append(f)
            continue
        stripped = f.content.lstrip()
        prefix = f.content[: len(f.content) - len(stripped)]
        _, frontmatter, body = stripped.split(_FRONTMATTER_DELIM, 2)
        frontmatter = re.sub(
            r"(?m)^(\s*name:).*$",
            lambda m: f"{m.group(1)} {new_name}",
            frontmatter,
            count=1,
        )
        rebuilt = (
            prefix
            + _FRONTMATTER_DELIM
            + frontmatter
            + _FRONTMATTER_DELIM
            + body
        )
        out.append(SkillFile(relative_path=f.relative_path, content=rebuilt))
    return out


async def list_skills_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
) -> list[SkillModel]:
    """Return global seed skills plus the org's custom skills, seeds first."""
    query = (
        select(SkillModel)
        .where(or_(SkillModel.org_id == org_id, SkillModel.org_id.is_(None)))
        .order_by(SkillModel.is_seed.desc(), SkillModel.name)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_skill_core(
    session: AsyncSession,
    skill_id: str,
    *,
    org_id: str | None = None,
) -> SkillModel:
    """Fetch one skill visible to ``org_id`` (its own or a global seed)."""
    result = await session.execute(select(SkillModel).where(SkillModel.id == skill_id))
    skill = result.scalar_one_or_none()
    if skill is None or (skill.org_id is not None and skill.org_id != org_id):
        raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")
    return skill


def _validate_result_focus(result_focus: str | None) -> None:
    """Raise HTTPException(422) if a JSON-schema result_focus is malformed."""
    spec = parse_result_focus(result_focus)
    if spec is not None:
        try:
            normalize_findings_schema(spec)
        except UnsupportedSchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _resolve_skill_name(
    session: AsyncSession, base_name: str, *, org_id: str | None
) -> str:
    """Smallest free name in ``base``, ``base-2``, ``base-3``, … within the
    org's uniqueness bucket (matching ``idx_skills_unique_org_name``). The
    soft-delete auto-filter excludes tombstoned rows, so reused names are free.
    """
    result = await session.execute(
        select(SkillModel.name).where(
            func.coalesce(SkillModel.org_id, "") == (org_id or "")
        )
    )
    existing = {row[0] for row in result.all()}
    if base_name not in existing:
        return base_name
    n = 2
    while f"{base_name}-{n}" in existing:
        n += 1
    return f"{base_name}-{n}"


async def create_skill_core(
    session: AsyncSession,
    *,
    data: SkillCreate,
    org_id: str | None = None,
    user_id: str | None = None,
) -> SkillModel:
    """Create a custom skill owned by ``org_id``, validating its SKILL.md.

    On a name collision within the org, the skill is stored under a
    version-suffixed name (``my-skill`` → ``my-skill-2`` → …) and its SKILL.md
    frontmatter ``name:`` is rewritten to match.
    """
    base_name, description = parse_skill(data.files)
    _validate_result_focus(data.result_focus)
    name = await _resolve_skill_name(session, base_name, org_id=org_id)
    files = data.files if name == base_name else _rewrite_skill_name(data.files, name)
    skill = SkillModel(
        org_id=org_id,
        created_by_user_id=user_id,
        name=name,
        description=description,
        is_seed=False,
        operator_prompt=data.operator_prompt,
        result_focus=data.result_focus,
        evaluation_metric=data.evaluation_metric,
        files=[
            SkillFileModel(relative_path=f.relative_path, content=f.content)
            for f in files
        ],
    )
    session.add(skill)
    await session.flush()
    return skill


async def _get_owned_skill(
    session: AsyncSession,
    *,
    skill_id: str,
    org_id: str | None,
) -> SkillModel:
    """Fetch + authorize a skill for mutation. 403 for seeds, 404 otherwise."""
    result = await session.execute(select(SkillModel).where(SkillModel.id == skill_id))
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")
    if skill.is_seed:
        raise HTTPException(status_code=403, detail="Seed skills are read-only")
    if skill.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")
    return skill


async def update_skill_core(
    session: AsyncSession,
    skill_id: str,
    *,
    data: SkillUpdate,
    org_id: str | None = None,
) -> SkillModel:
    """Apply provided fields to an org-owned skill. ``files`` replaces the set."""
    skill = await _get_owned_skill(session, skill_id=skill_id, org_id=org_id)
    payload = data.model_dump(exclude_unset=True)
    if "files" in payload and data.files is not None:
        name, description = parse_skill(data.files)
        skill.name = name
        skill.description = description
        skill.files = [
            SkillFileModel(relative_path=f.relative_path, content=f.content)
            for f in data.files
        ]
    else:
        if data.name is not None:
            skill.name = data.name
        if data.description is not None:
            skill.description = data.description
    if "result_focus" in payload:
        _validate_result_focus(data.result_focus)
        skill.result_focus = data.result_focus
    if "operator_prompt" in payload:
        skill.operator_prompt = data.operator_prompt
    if "evaluation_metric" in payload:
        skill.evaluation_metric = data.evaluation_metric
    await session.flush()
    return skill


async def delete_skill_core(
    session: AsyncSession,
    skill_id: str,
    *,
    org_id: str | None = None,
) -> None:
    """Soft-delete an org-owned skill."""
    skill = await _get_owned_skill(session, skill_id=skill_id, org_id=org_id)
    skill.deleted_at = utcnow()
    await session.flush()
