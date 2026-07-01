from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routers.tasks import _resolve_connected_user
from auth import AuthContext, require_auth
from auth.provisioning import (
    ClerkGithubIdentity,
    _set_github_id_if_absent,
    fetch_github_identity_from_clerk,
)
from models import APIKeyScope, UserModel
from oddish.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github", tags=["GitHub"])


class GitHubLinkageResponse(BaseModel):
    linked: bool
    user_id: str | None = None


async def _fetch_clerk_identity(clerk_user_id: str) -> ClerkGithubIdentity:
    # Single monkeypatchable seam: ONE Clerk GET returns both handle and
    # github_id (fetching them separately would double the Clerk load per member).
    return await fetch_github_identity_from_clerk(clerk_user_id)


async def _refresh_org_github_identities(session: AsyncSession, *, org_id: str) -> None:
    """Re-pull github_username AND github_id from Clerk for active org members so
    a renamed handle (or an un-backfilled github_id) can re-match. Deliberately
    NOT ensure_user_github_identity / _refresh_user_github_identity — those
    early-return once a handle is set (M2). Fail-open: a Clerk error for any user
    keeps its stale values, never raises. Cost: one Clerk GET per active member,
    only on a lookup miss.
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
            identity = await _fetch_clerk_identity(user.clerk_user_id)
        except Exception:  # fail-open: any Clerk error keeps the stale values
            logger.warning(
                "linkage refresh: Clerk fetch failed for user %s", user.id, exc_info=True
            )
            continue
        if identity.username and identity.username != user.github_username:
            user.github_username = identity.username
        # Backfill github_id only when absent; collision-safe (include_deleted
        # pre-check) via the shared provisioning helper — never overwrites a
        # differing existing id.
        await _set_github_id_if_absent(session, user, identity.github_id)


@router.get("/linkage", response_model=GitHubLinkageResponse)
async def github_linkage(
    auth: Annotated[AuthContext, Depends(require_auth)],
    handle: Annotated[str | None, Query()] = None,
    actor_id: Annotated[str | None, Query()] = None,
) -> GitHubLinkageResponse:
    """Is the pusher a CONNECTED (exactly-one active, Clerk-linked) member of the
    caller's org? Prefers the immutable ``actor_id`` (github_id) over the mutable
    ``handle`` via the SAME shared resolver /tasks/sweep owner resolution uses
    (predicate parity, INV2). Authorizes by org + READ scope only — never a human
    identity from the API key (M4). Best-effort / fail-open: a stale stored handle
    (or an un-backfilled github_id) is refreshed from Clerk on a miss, and a Clerk
    outage falls back to the stored values rather than reporting unlinked.
    """
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        user = await _resolve_connected_user(
            session,
            org_id=auth.org_id,
            github_id=actor_id,
            github_username=handle,
        )
        if user is None:
            # Best-effort refresh writes handles + backfills github_id. Flush here
            # so a collision race / DB error surfaces now and rolls back — the
            # linkage answer must never 500 on a refresh side-effect (fail-open);
            # on failure we answer from committed/stored state.
            try:
                await _refresh_org_github_identities(session, org_id=auth.org_id)
                await session.flush()
            except Exception:
                await session.rollback()
                logger.warning(
                    "linkage refresh failed; answering from stored state",
                    exc_info=True,
                )
            user = await _resolve_connected_user(
                session,
                org_id=auth.org_id,
                github_id=actor_id,
                github_username=handle,
            )
        if user is None:
            return GitHubLinkageResponse(linked=False)
        return GitHubLinkageResponse(linked=True, user_id=user.id)
