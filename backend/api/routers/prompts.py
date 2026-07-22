"""CRUD and activation endpoints for the shared prompt registry."""

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

router = APIRouter(tags=["Prompts"])


def _to_response(prompt, version) -> PromptResponse:
    response = PromptResponse.model_validate(prompt)
    response.content = version.content if version is not None else None
    return response


@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return [PromptResponse.model_validate(p) for p in await list_prompts_core(session)]


@router.get("/prompts/{key}", response_model=PromptResponse)
async def get_prompt(
    key: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    version: Annotated[int | None, Query()] = None,
) -> PromptResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompt, item = await get_prompt_core(session, key, version=version)
        return _to_response(prompt, item)


@router.get("/prompts/{key}/versions", response_model=list[PromptVersionResponse])
async def get_versions(
    key: str, auth: Annotated[AuthContext, Depends(require_auth)]
) -> list[PromptVersionResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return [
            PromptVersionResponse.model_validate(v)
            for v in await list_prompt_versions_core(session, key)
        ]


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
        prompt, item = await get_prompt_core(session, key)
        return _to_response(prompt, item)


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
        prompt, item = await get_prompt_core(session, key)
        return _to_response(prompt, item)
