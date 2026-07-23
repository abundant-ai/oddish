"""CRUD + versioning endpoints for the analyzer prompt registry.

Thin wrapper over ``oddish.core.prompts``: authenticate, open a session,
delegate, commit, serialize. ``kind`` path params accept a built-in
``PromptKind`` value (UPPERCASE) or a lowercase-slug custom kind (saved
prompts for ``oddish qa`` variants); anything else 422s at the boundary
while the core stays string-typed."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.prompts import (
    get_prompt_core,
    get_prompt_usage_core,
    list_prompt_versions_core,
    list_prompts_core,
    set_prompt_core,
)
from oddish.db import PromptKind, get_session
from oddish.schemas import (
    PromptResponse,
    PromptSetRequest,
    PromptUsage,
    PromptVersionResponse,
)

router = APIRouter()

# Custom kinds are lowercase slugs so they can never collide with (or spoof)
# the UPPERCASE built-in vocabulary.
_CUSTOM_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _validated_kind(kind: str) -> str:
    if kind in {k.value for k in PromptKind} or _CUSTOM_KIND_RE.fullmatch(kind):
        return kind
    raise HTTPException(
        status_code=422,
        detail=(
            "kind must be a built-in prompt kind "
            f"({', '.join(k.value for k in PromptKind)}) or a lowercase slug"
        ),
    )


async def _validated_ref(session, ref: str) -> str:
    """Accept an existing prompt id, otherwise enforce the kind vocabulary."""
    try:
        await get_prompt_core(session, ref)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return _validated_kind(ref)
    return ref


def _latest_of(versions) -> int | None:
    return max((v.version for v in versions), default=None)


def _to_response(prompt, version) -> PromptResponse:
    resp = PromptResponse.model_validate(prompt)
    resp.latest_version = _latest_of(prompt.versions)
    if version is not None:
        resp.version = version.version
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
            resp.latest_version = _latest_of(p.versions)
            out.append(resp)
        return out


@router.get("/prompts/{key_or_id}", response_model=PromptResponse)
async def get_prompt(
    key_or_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    version: Annotated[int | None, Query()] = None,
) -> PromptResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        ref = await _validated_ref(session, key_or_id)
        prompt, ver = await get_prompt_core(session, ref, version=version)
        response = _to_response(prompt, ver)
        response.usage = PromptUsage(**await get_prompt_usage_core(session, ref))
        return response


@router.get("/prompts/{key_or_id}/versions", response_model=list[PromptVersionResponse])
async def get_prompt_versions(
    key_or_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[PromptVersionResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        ref = await _validated_ref(session, key_or_id)
        versions = await list_prompt_versions_core(session, ref)
        return [PromptVersionResponse.model_validate(v) for v in versions]


@router.put("/prompts/{key_or_id}", response_model=PromptResponse)
async def set_prompt(
    key_or_id: str,
    data: PromptSetRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PromptResponse:
    # FULL, not TASKS: prompts are a single global registry that drives QA
    # for every org, so any org's TASKS key must not be able to rewrite what
    # every other org's analysis runs on.
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        ref = await _validated_ref(session, key_or_id)
        await set_prompt_core(
            session,
            kind=ref,
            content=data.content,
            description=data.description,
            created_by=auth.user_id,
        )
        await session.commit()
        prompt, ver = await get_prompt_core(session, ref)
        return _to_response(prompt, ver)
