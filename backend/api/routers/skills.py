"""CRUD endpoints for shared skills.

Thin wrapper over ``oddish.core.skills``: authenticate, open a session,
delegate to the core function, commit, serialize. Seeds are read-only
(the core layer raises 403 on mutation)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.skills import (
    create_skill_core,
    delete_skill_core,
    get_skill_core,
    list_skills_core,
    update_skill_core,
)
from oddish.db import get_session
from oddish.schemas import SkillCreate, SkillResponse, SkillUpdate

router = APIRouter()


@router.get("/skills", response_model=list[SkillResponse])
async def list_skills(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[SkillResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        skills = await list_skills_core(session, org_id=auth.org_id)
        return [SkillResponse.model_validate(s) for s in skills]


@router.get("/skills/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> SkillResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        skill = await get_skill_core(session, skill_id, org_id=auth.org_id)
        return SkillResponse.model_validate(skill)


@router.post("/skills", response_model=SkillResponse)
async def create_skill(
    data: SkillCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> SkillResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        skill = await create_skill_core(
            session, data=data, org_id=auth.org_id, user_id=auth.user_id
        )
        await session.commit()
        return SkillResponse.model_validate(skill)


@router.put("/skills/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: str,
    data: SkillUpdate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> SkillResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        skill = await update_skill_core(
            session, skill_id, data=data, org_id=auth.org_id
        )
        await session.commit()
        return SkillResponse.model_validate(skill)


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict[str, bool]:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await delete_skill_core(session, skill_id, org_id=auth.org_id)
        await session.commit()
        return {"ok": True}
