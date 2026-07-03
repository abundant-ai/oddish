from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api.routers.task_submission import resolve_connected_user
from auth import AuthContext, require_auth
from models import APIKeyScope
from oddish.db import get_session

router = APIRouter(prefix="/github", tags=["GitHub"])


class GitHubLinkageResponse(BaseModel):
    linked: bool
    user_id: str | None = None


@router.get("/linkage", response_model=GitHubLinkageResponse)
async def github_linkage(
    auth: Annotated[AuthContext, Depends(require_auth)],
    handle: Annotated[str | None, Query()] = None,
    actor_id: Annotated[str | None, Query()] = None,
) -> GitHubLinkageResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        user = await resolve_connected_user(
            session, org_id=auth.org_id, github_id=actor_id, github_username=handle
        )
    if user is None:
        return GitHubLinkageResponse(linked=False)
    return GitHubLinkageResponse(linked=True, user_id=user.id)
