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
from oddish.db import ExperimentModel, PromptKind, TaskModel, TrialModel, get_session
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
        prompts = await list_prompts_core(session, org_id=auth.org_id)
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
    scope: Annotated[str, Query()] = "org",
    scope_id: Annotated[str | None, Query()] = None,
) -> PromptResponse:
    """Append a scoped prompt version.

    ``org`` and ``user`` infer the current auth identity. Domain scopes require
    an id and are checked against the active organization. ``global`` preserves
    the legacy installation-wide registry and requires FULL scope.
    """
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        ref = await _validated_ref(session, key_or_id)
        resolved_scope: str | None
        resolved_scope_id: str | None
        if scope == "global":
            resolved_scope = resolved_scope_id = None
        elif scope == "org":
            resolved_scope, resolved_scope_id = "org", auth.org_id
        elif scope == "user":
            if not auth.user_id:
                raise HTTPException(status_code=422, detail="user scope requires user auth")
            resolved_scope, resolved_scope_id = "user", auth.user_id
        elif scope in {"experiment", "task", "trial"}:
            if not scope_id:
                raise HTTPException(status_code=422, detail=f"{scope} scope requires scope_id")
            model = {
                "experiment": ExperimentModel,
                "task": TaskModel,
                "trial": TrialModel,
            }[scope]
            target = await session.get(model, scope_id)
            if target is None or target.org_id != auth.org_id:
                raise HTTPException(status_code=404, detail=f"{scope} not found")
            resolved_scope, resolved_scope_id = scope, scope_id
        else:
            raise HTTPException(
                status_code=422,
                detail="scope must be global, org, user, experiment, task, or trial",
            )
        await set_prompt_core(
            session,
            kind=ref,
            content=data.content,
            description=data.description,
            created_by=auth.user_id,
            scope_type=resolved_scope,
            scope_id=resolved_scope_id,
            org_id=auth.org_id if resolved_scope is not None else None,
        )
        await session.commit()
        prompt, ver = await get_prompt_core(
            session,
            ref,
            scope_type=resolved_scope,
            scope_id=resolved_scope_id,
        )
        return _to_response(prompt, ver)
