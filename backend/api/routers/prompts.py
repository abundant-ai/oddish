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


@router.get("/prompts/{key_or_id}", response_model=PromptResponse)
async def get_prompt(
    key_or_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    version: Annotated[int | None, Query()] = None,
) -> PromptResponse:
    """Fetch a prompt by its ``key`` or its ``id``."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, key_or_id, version=version)
        return _to_response(prompt, ver)


@router.get("/prompts/{key_or_id}/versions", response_model=list[PromptVersionResponse])
async def get_prompt_versions(
    key_or_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptVersionResponse]:
    """List all versions of a prompt, addressed by ``key`` or ``id``."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        versions = await list_prompt_versions_core(session, key_or_id)
        return [PromptVersionResponse.model_validate(v) for v in versions]


@router.put("/prompts/{key_or_id}", response_model=PromptResponse)
async def set_prompt(
    key_or_id: str,
    data: PromptSetRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    """Append (and by default activate) a new version. An unknown key_or_id
    creates a brand-new prompt keyed by that value; a known key or id appends
    to the existing prompt."""
    # FULL, not TASKS: prompts are a single global registry that drives QA
    # for every org, so any org's TASKS key must not be able to rewrite what
    # every other org's analysis runs on.
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        await set_prompt_core(
            session,
            key=key_or_id,
            content=data.content,
            description=data.description,
            activate=data.activate,
            created_by=auth.user_id,
        )
        await session.commit()
        prompt, ver = await get_prompt_core(session, key_or_id)
        return _to_response(prompt, ver)


@router.post("/prompts/{key_or_id}/activate", response_model=PromptResponse)
async def activate_prompt(
    key_or_id: str,
    data: PromptActivateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    """Point the active version at an existing version, addressed by ``key`` or ``id``."""
    # FULL, not TASKS: see set_prompt above.
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        await activate_prompt_version_core(session, key_or_id, data.version)
        await session.commit()
        prompt, ver = await get_prompt_core(session, key_or_id)
        return _to_response(prompt, ver)
