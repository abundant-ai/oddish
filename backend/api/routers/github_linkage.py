from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routers.tasks import _lookup_user_by_github_username
from auth import AuthContext, require_auth
from auth.provisioning import fetch_github_identity_from_clerk
from models import APIKeyScope, UserModel
from oddish.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github", tags=["GitHub"])


class GitHubLinkageResponse(BaseModel):
    linked: bool
    user_id: str | None = None


async def _fetch_clerk_github_username(clerk_user_id: str) -> str | None:
    username, _email = await fetch_github_identity_from_clerk(clerk_user_id)
    return username


async def _refresh_org_github_handles(session: AsyncSession, *, org_id: str) -> None:
    """Re-pull github_username from Clerk for active org members so a renamed
    handle can re-match. Deliberately NOT ensure_user_github_identity /
    _refresh_user_github_identity — those early-return once a handle is set (M2).
    Fail-open: a Clerk error for any user keeps its stale handle, never raises.
    Cost: one Clerk GET per active member, only on a lookup miss; an immutable
    github_id (deferred) is the durable fix for renames and removes this scan.
    """
    result = await session.execute(
        select(UserModel).where(
            UserModel.org_id == org_id,
            UserModel.is_active == True,  # noqa: E712
            UserModel.clerk_user_id.is_not(None),
        )
    )
    for user in result.scalars().all():
        try:
            handle = await _fetch_clerk_github_username(user.clerk_user_id)
        except Exception:  # fail-open: any Clerk error keeps the stale handle
            logger.warning(
                "linkage refresh: Clerk fetch failed for user %s", user.id, exc_info=True
            )
            continue
        if handle and handle != user.github_username:
            user.github_username = handle


@router.get("/linkage", response_model=GitHubLinkageResponse)
async def github_linkage(
    auth: Annotated[AuthContext, Depends(require_auth)],
    handle: Annotated[str, Query()],
) -> GitHubLinkageResponse:
    """Is GitHub ``handle`` a CONNECTED (exactly-one active, Clerk-linked) member
    of the caller's org? Shares the exactly-one predicate with /tasks/sweep owner
    resolution (predicate parity). Authorizes by org + READ scope only — never a
    human identity from the API key (M4). Best-effort / fail-open: a stale stored
    handle is refreshed from Clerk on a miss, and a Clerk outage falls back to the
    stored value rather than reporting unlinked.
    """
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        user = await _lookup_user_by_github_username(
            session, github_username=handle, org_id=auth.org_id
        )
        if user is None:
            await _refresh_org_github_handles(session, org_id=auth.org_id)
            user = await _lookup_user_by_github_username(
                session, github_username=handle, org_id=auth.org_id
            )
        if user is None:
            return GitHubLinkageResponse(linked=False)
        return GitHubLinkageResponse(linked=True, user_id=user.id)
