"""CRUD + versioning endpoints for the analyzer prompt registry.

Thin wrapper over ``oddish.core.prompts``: authenticate, open a session,
delegate, commit, serialize."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.prompts import (
    activate_prompt_version_core,
    get_prompt_core,
    list_prompt_versions_core,
    list_prompts_core,
    set_prompt_core,
)
from oddish.db import get_session
from oddish.schemas import (
    PromptActivateRequest,
    PromptResponse,
    PromptSetRequest,
    PromptVersionResponse,
)

router = APIRouter()


def _to_response(prompt, version) -> PromptResponse:
    resp = PromptResponse.model_validate(prompt)
    resp.content = version.content if version is not None else None
    return resp


@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompts = await list_prompts_core(session)
        return [PromptResponse.model_validate(p) for p in prompts]


@router.get("/prompts/{key}", response_model=PromptResponse)
async def get_prompt(
    key: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    version: Annotated[int | None, Query()] = None,
) -> PromptResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, key, version=version)
        return _to_response(prompt, ver)


@router.get("/prompts/{key}/versions", response_model=list[PromptVersionResponse])
async def get_prompt_versions(
    key: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptVersionResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        versions = await list_prompt_versions_core(session, key)
        return [PromptVersionResponse.model_validate(v) for v in versions]


@router.put("/prompts/{key}", response_model=PromptResponse)
async def set_prompt(
    key: str,
    data: PromptSetRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        await set_prompt_core(
            session,
            key=key,
            content=data.content,
            description=data.description,
            activate=data.activate,
            created_by=auth.user_id,
        )
        await session.commit()
        prompt, ver = await get_prompt_core(session, key)
        return _to_response(prompt, ver)


@router.post("/prompts/{key}/activate", response_model=PromptResponse)
async def activate_prompt(
    key: str,
    data: PromptActivateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        await activate_prompt_version_core(session, key, data.version)
        await session.commit()
        prompt, ver = await get_prompt_core(session, key)
        return _to_response(prompt, ver)
