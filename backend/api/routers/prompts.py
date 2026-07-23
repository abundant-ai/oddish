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
from auth.permissions import require_operator_org
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


def _assert_org_access(prompt, auth: AuthContext) -> None:
    """A prompt id is resolvable across scopes, so every id-resolved row must be
    re-checked against the caller's org. 404 rather than 403: a foreign prompt's
    existence is itself not the caller's to learn."""
    if prompt.org_id and prompt.org_id != auth.org_id:
        raise HTTPException(status_code=404, detail="Prompt not found")


async def _validated_ref(session, ref: str, auth: AuthContext):
    """Accept an existing prompt id, otherwise enforce the kind vocabulary.

    Returns ``(ref, prompt)`` -- ``prompt`` is the row ``ref`` already names
    (``None`` for a brand-new kind), so write callers can check its actual
    scope before mutating it.
    """
    try:
        prompt, _ = await get_prompt_core(session, ref)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return _validated_kind(ref), None
    _assert_org_access(prompt, auth)
    return ref, prompt


def _latest_of(versions) -> int | None:
    return max((v.version for v in versions), default=None)


def _to_response(prompt, version) -> PromptResponse:
    resp = PromptResponse.model_validate(prompt)
    resp.latest_version = _latest_of(prompt.versions)
    if version is not None:
        resp.version = version.version
        resp.content = version.content
    return resp


def _resolve_scope_params(
    scope: str | None, scope_id: str | None, auth: AuthContext
) -> tuple[str | None, str | None]:
    """Map read-side scope query params onto (scope_type, scope_id).

    Read-only: unlike ``set_prompt`` this does not verify the target exists,
    because a miss resolves to 404 from the core lookup anyway.
    """
    if scope in (None, "global"):
        return None, None
    if scope == "org":
        return "org", auth.org_id
    if scope == "user":
        if not auth.user_id:
            raise HTTPException(status_code=422, detail="user scope requires user auth")
        return "user", auth.user_id
    if scope in {"experiment", "task", "trial"}:
        if not scope_id:
            raise HTTPException(
                status_code=422, detail=f"{scope} scope requires scope_id"
            )
        return scope, scope_id
    raise HTTPException(
        status_code=422,
        detail="scope must be global, org, user, experiment, task, or trial",
    )


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
    scope: Annotated[str | None, Query()] = None,
    scope_id: Annotated[str | None, Query()] = None,
) -> PromptResponse:
    auth.require_scope(APIKeyScope.READ)
    scope_type, resolved_scope_id = _resolve_scope_params(scope, scope_id, auth)
    async with get_session() as session:
        ref, _ = await _validated_ref(session, key_or_id, auth)
        prompt, ver = await get_prompt_core(
            session,
            ref,
            version=version,
            scope_type=scope_type,
            scope_id=resolved_scope_id,
        )
        _assert_org_access(prompt, auth)
        response = _to_response(prompt, ver)
        response.usage = PromptUsage(**await get_prompt_usage_core(session, prompt.id))
        return response


@router.get("/prompts/{key_or_id}/versions", response_model=list[PromptVersionResponse])
async def get_prompt_versions(
    key_or_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    scope: Annotated[str | None, Query()] = None,
    scope_id: Annotated[str | None, Query()] = None,
) -> list[PromptVersionResponse]:
    auth.require_scope(APIKeyScope.READ)
    scope_type, resolved_scope_id = _resolve_scope_params(scope, scope_id, auth)
    async with get_session() as session:
        ref, _ = await _validated_ref(session, key_or_id, auth)
        prompt, _ = await get_prompt_core(
            session, ref, scope_type=scope_type, scope_id=resolved_scope_id
        )
        _assert_org_access(prompt, auth)
        versions = await list_prompt_versions_core(
            session, ref, scope_type=scope_type, scope_id=resolved_scope_id
        )
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
    the legacy installation-wide registry, requires FULL scope, and -- since it
    is shared by every tenant -- is further gated to the platform operator.
    """
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        ref, existing_prompt = await _validated_ref(session, key_or_id, auth)
        resolved_scope: str | None
        resolved_scope_id: str | None
        if scope == "global":
            require_operator_org(auth)
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
        if (
            existing_prompt is not None
            and existing_prompt.kind != ref
            and (existing_prompt.scope_type, existing_prompt.scope_id)
            != (resolved_scope, resolved_scope_id)
        ):
            # `ref` resolved to an existing row by id (its kind differs from
            # the ref we looked up), but at a different scope than requested --
            # e.g. a global/other-scope prompt id combined with ?scope=org.
            # Appending here would silently mutate that row instead of
            # creating (or writing to) the requested scope's own row.
            raise HTTPException(status_code=404, detail="Prompt not found")
        try:
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
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        prompt, ver = await get_prompt_core(
            session,
            ref,
            scope_type=resolved_scope,
            scope_id=resolved_scope_id,
        )
        return _to_response(prompt, ver)
