"""Shared mapping from request scope params onto (scope_type, scope_id).

Both the prompt registry and QA job assignments accept the same six scopes, and
both must check domain ids against the caller's organization before writing.
One implementation, so a fix to the tenancy check cannot land in only one of
them.
"""

from __future__ import annotations

from fastapi import HTTPException

from auth import AuthContext
from auth.permissions import require_operator_org
from oddish.db import ExperimentModel, TaskModel, TrialModel

_DOMAIN_MODELS = {
    "experiment": ExperimentModel,
    "task": TaskModel,
    "trial": TrialModel,
}

_SCOPE_HELP = "scope must be global, org, user, experiment, task, or trial"


async def resolve_read_scope(
    session, scope: str | None, scope_id: str | None, auth: AuthContext
) -> tuple[str | None, str | None]:
    """Read-side mapping. ``global`` is ``(None, None)``, matching how the
    prompt registry spells the installation-wide row.

    Domain targets are verified against the active organization before their
    ids are used in a scoped lookup.
    """
    if scope in (None, "global"):
        return None, None
    if scope == "org":
        return "org", auth.org_id
    if scope == "user":
        if not auth.user_id:
            raise HTTPException(status_code=422, detail="user scope requires user auth")
        return "user", auth.user_id
    if scope in _DOMAIN_MODELS:
        if not scope_id:
            raise HTTPException(
                status_code=422, detail=f"{scope} scope requires scope_id"
            )
        target = await session.get(_DOMAIN_MODELS[scope], scope_id)
        if target is None or target.org_id != auth.org_id:
            raise HTTPException(status_code=404, detail=f"{scope} not found")
        return scope, scope_id
    raise HTTPException(status_code=422, detail=_SCOPE_HELP)


async def resolve_write_scope(
    session, scope: str, scope_id: str | None, auth: AuthContext
) -> tuple[str | None, str | None]:
    """Write-side mapping, with the checks reads do not need.

    ``org``/``user`` infer the caller's identity rather than trusting a
    supplied id. Domain ids are verified to exist *and* to belong to the active
    organization. ``global`` is shared by every tenant, so it is gated to the
    platform operator -- not merely to ``APIKeyScope.FULL``, which is a
    per-org scope every tenant's admin key satisfies.
    """
    if scope == "global":
        require_operator_org(auth)
        return None, None
    if scope == "org":
        return "org", auth.org_id
    if scope == "user":
        if not auth.user_id:
            raise HTTPException(status_code=422, detail="user scope requires user auth")
        return "user", auth.user_id
    if scope in _DOMAIN_MODELS:
        if not scope_id:
            raise HTTPException(
                status_code=422, detail=f"{scope} scope requires scope_id"
            )
        target = await session.get(_DOMAIN_MODELS[scope], scope_id)
        if target is None or target.org_id != auth.org_id:
            raise HTTPException(status_code=404, detail=f"{scope} not found")
        return scope, scope_id
    raise HTTPException(status_code=422, detail=_SCOPE_HELP)
