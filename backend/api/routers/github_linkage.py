from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routers.task_submission import _resolve_connected_user
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

_REFRESH_CONCURRENCY = 8


class GitHubLinkageResponse(BaseModel):
    linked: bool
    user_id: str | None = None


async def _fetch_clerk_identity(clerk_user_id: str) -> ClerkGithubIdentity:
    return await fetch_github_identity_from_clerk(clerk_user_id)


async def _load_refresh_targets(
    session: AsyncSession, *, org_id: str
) -> list[tuple[str, str]]:
    result = await session.execute(
        select(UserModel.id, UserModel.clerk_user_id).where(
            UserModel.org_id == org_id,
            UserModel.is_active == True,  # noqa: E712
            UserModel.clerk_user_id.is_not(None),
        )
    )
    return [(uid, cid) for uid, cid in result.all()]


async def _fetch_org_identities(
    targets: list[tuple[str, str]],
) -> dict[str, ClerkGithubIdentity]:
    # Do not hold a DB session while fanning out to Clerk.
    semaphore = asyncio.Semaphore(_REFRESH_CONCURRENCY)

    async def _one(user_id: str, clerk_user_id: str):
        async with semaphore:
            try:
                return user_id, await _fetch_clerk_identity(clerk_user_id)
            except Exception:  # fail-open: any Clerk error keeps the stale values
                logger.warning(
                    "linkage refresh: Clerk fetch failed for user %s",
                    user_id,
                    exc_info=True,
                )
                return user_id, None

    pairs = await asyncio.gather(*(_one(uid, cid) for uid, cid in targets))
    return {uid: identity for uid, identity in pairs if identity is not None}


async def _apply_org_identities(
    session: AsyncSession, identities: dict[str, ClerkGithubIdentity]
) -> None:
    if not identities:
        return
    result = await session.execute(
        select(UserModel).where(UserModel.id.in_(list(identities)))
    )
    for user in result.scalars().all():
        identity = identities.get(user.id)
        if identity is None:
            continue
        if identity.username and identity.username != user.github_username:
            user.github_username = identity.username
        await _set_github_id_if_absent(session, user, identity.github_id)


@router.get("/linkage", response_model=GitHubLinkageResponse)
async def github_linkage(
    auth: Annotated[AuthContext, Depends(require_auth)],
    handle: Annotated[str | None, Query()] = None,
    actor_id: Annotated[str | None, Query()] = None,
) -> GitHubLinkageResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        user = await _resolve_connected_user(
            session, org_id=auth.org_id, github_id=actor_id, github_username=handle
        )
        if user is not None:
            return GitHubLinkageResponse(linked=True, user_id=user.id)
        targets = await _load_refresh_targets(session, org_id=auth.org_id)

    try:
        identities = await _fetch_org_identities(targets)
    except Exception:  # fail-open: never 500 on a refresh side-effect
        logger.warning(
            "linkage refresh fetch failed; answering from stored state", exc_info=True
        )
        identities = {}

    async with get_session() as session:
        # Fail-open on refresh write errors.
        try:
            await _apply_org_identities(session, identities)
            await session.flush()
        except Exception:
            await session.rollback()
            logger.warning(
                "linkage refresh apply failed; answering from stored state",
                exc_info=True,
            )
        user = await _resolve_connected_user(
            session, org_id=auth.org_id, github_id=actor_id, github_username=handle
        )
    if user is None:
        return GitHubLinkageResponse(linked=False)
    return GitHubLinkageResponse(linked=True, user_id=user.id)
