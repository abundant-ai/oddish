"""CRUD + versioning endpoints for the analyzer prompt registry.

Thin wrapper over ``oddish.core.prompts``: authenticate, open a session,
delegate, commit, serialize. ``kind`` path params are typed ``PromptKind``
so unknown kinds 422 at the boundary while the core stays string-typed."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.prompts import (
    get_prompt_core,
    list_prompt_versions_core,
    list_prompts_core,
    set_prompt_core,
)
from oddish.db import PromptKind, get_session
from oddish.schemas import (
    PromptResponse,
    PromptSetRequest,
    PromptVersionResponse,
)

router = APIRouter()


def _to_response(prompt, version) -> PromptResponse:
    resp = PromptResponse.model_validate(prompt)
    if version is not None:
        resp.latest_version = version.version
        resp.content = version.content
    return resp


@router.get("/prompts", response_model=list[PromptResponse])
async def list_prompts(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompts = await list_prompts_core(session)
        out = []
        for p in prompts:
            resp = PromptResponse.model_validate(p)
            resp.latest_version = max((v.version for v in p.versions), default=None)
            out.append(resp)
        return out


@router.get("/prompts/{kind}", response_model=PromptResponse)
async def get_prompt(
    kind: PromptKind,
    auth: Annotated[AuthContext, Depends(require_auth)],
    version: Annotated[int | None, Query()] = None,
) -> PromptResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, kind.value, version=version)
        return _to_response(prompt, ver)


@router.get("/prompts/{kind}/versions", response_model=list[PromptVersionResponse])
async def get_prompt_versions(
    kind: PromptKind,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptVersionResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        versions = await list_prompt_versions_core(session, kind.value)
        return [PromptVersionResponse.model_validate(v) for v in versions]


@router.put("/prompts/{kind}", response_model=PromptResponse)
async def set_prompt(
    kind: PromptKind,
    data: PromptSetRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    # FULL, not TASKS: prompts are a single global registry that drives QA
    # for every org, so any org's TASKS key must not be able to rewrite what
    # every other org's analysis runs on.
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        await set_prompt_core(
            session,
            kind=kind.value,
            content=data.content,
            description=data.description,
            created_by=auth.user_id,
        )
        await session.commit()
        prompt, ver = await get_prompt_core(session, kind.value)
        return _to_response(prompt, ver)
