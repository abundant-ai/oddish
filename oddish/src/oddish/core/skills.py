"""Core logic for shared skills.

Validation (`parse_skill`) is pure (no DB) so it can be unit-tested directly.
CRUD follows the repo's router->core layering: functions receive an AsyncSession and never commit — the calling router owns the transaction.
"""

from __future__ import annotations

import yaml
from fastapi import HTTPException
from sqlalchemy import or_, select
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


async def create_skill_core(
    session: AsyncSession,
    *,
    data: SkillCreate,
    org_id: str | None = None,
    user_id: str | None = None,
) -> SkillModel:
    """Create a custom skill owned by ``org_id``, validating its SKILL.md."""
    name, description = parse_skill(data.files)
    _validate_result_focus(data.result_focus)
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
            for f in data.files
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
