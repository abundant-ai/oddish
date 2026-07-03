from __future__ import annotations

import logging
import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update

from api.schemas import (
    InviteUserRequest,
    InviteUserResponse,
    OrganizationResponse,
    UserResponse,
)
from auth import AuthContext, AuthMethod, require_admin, require_auth
from auth.verification import invalidate_cached_clerk_auth
from models import UserModel, UserRole
from oddish.core.tags.ownership_transfer import transfer_tag_ownership_to_admin
from oddish.db import get_session, utcnow

logger = logging.getLogger(__name__)

CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")

router = APIRouter(tags=["Organization"])


# =============================================================================
# Organization Endpoints
# =============================================================================


@router.get("/org", response_model=OrganizationResponse)
async def get_organization(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> OrganizationResponse:
    """Get the current organization."""
    if auth.org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    return OrganizationResponse(
        id=auth.org.id,
        name=auth.org.name,
        slug=auth.org.slug,
        plan=auth.org.plan,
        created_at=auth.org.created_at.isoformat(),
    )


# =============================================================================
# User Management
# =============================================================================


def _clerk_invite_role(role: UserRole) -> str:
    if role == UserRole.MEMBER:
        return "org:member"
    return "org:admin"


async def _create_clerk_invitation(
    clerk_org_id: str,
    email: str,
    role: UserRole,
) -> dict:
    if not CLERK_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="CLERK_SECRET_KEY not configured",
        )

    url = f"https://api.clerk.com/v1/organizations/{clerk_org_id}/invitations"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}
    payload = {"email_address": email, "role": _clerk_invite_role(role)}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text or "Failed to create Clerk invitation"
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"Failed to reach Clerk: {str(exc)}"
        )


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[UserResponse]:
    """List all users in the organization."""

    async with get_session() as session:
        result = await session.execute(
            select(UserModel)
            .where(UserModel.org_id == auth.org_id)
            .order_by(UserModel.created_at.desc())
        )
        users = result.scalars().all()

        return [
            UserResponse(
                id=u.id,
                email=u.email,
                name=u.name,
                github_username=u.github_username,
                github_id=u.github_id,
                role=u.role.value,
                org_id=u.org_id,
                created_at=u.created_at.isoformat(),
            )
            for u in users
        ]


@router.post("/users", response_model=InviteUserResponse)
async def invite_user(
    request: InviteUserRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> InviteUserResponse:
    """Invite a new user to the organization via Clerk."""

    # Validate role
    try:
        role = UserRole(request.role)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role: {request.role}. Must be one of: admin, member",
        )

    if not auth.org or not auth.org.clerk_org_id:
        raise HTTPException(
            status_code=400,
            detail="Organization is not linked to Clerk",
        )

    invitation = await _create_clerk_invitation(
        auth.org.clerk_org_id, request.email, role
    )

    return InviteUserResponse(
        invitation_id=invitation.get("id", ""),
        email=invitation.get("email_address", request.email),
        role=invitation.get("role", _clerk_invite_role(role)),
        status=invitation.get("status", "pending"),
    )


async def _delete_clerk_user(clerk_user_id: str) -> None:
    """Delete the user in Clerk. A 404 means the Clerk user is already gone,
    which is fine — we still proceed with local cleanup."""
    if not CLERK_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="CLERK_SECRET_KEY not configured",
        )

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.delete(url, headers=headers)
            if response.status_code == 404:
                return
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text or "Failed to delete Clerk user"
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"Failed to reach Clerk: {str(exc)}"
        )


async def _require_no_stranded_org(session, rows: list[UserModel]) -> None:
    """Reject self-deletion that would leave a *shared* org without any
    admin. Orgs where the deleter is the only active member (e.g. personal
    orgs) are exempt — otherwise personal-org users could never delete."""
    for row in rows:
        if row.role != UserRole.ADMIN:
            continue
        others = (
            (
                await session.execute(
                    select(UserModel.id, UserModel.role)
                    .where(UserModel.org_id == row.org_id)
                    .where(UserModel.id != row.id)
                    .where(UserModel.is_active == True)  # noqa: E712
                )
            )
            .all()
        )
        if others and not any(role == UserRole.ADMIN for _, role in others):
            raise HTTPException(
                status_code=400,
                detail=(
                    "You are the last admin of a workspace with other "
                    "members. Promote another admin before deleting your "
                    "account."
                ),
            )


@router.delete("/users/me")
async def delete_my_account(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Delete the calling user's account.

    Soft-deletes the local user rows first (committed before Clerk is
    touched), then deletes the Clerk user. If Clerk deletion fails the local
    rows are restored, so the account is never left in a state the user
    cannot retry from: a local failure aborts before Clerk is called, and a
    Clerk failure rolls the tombstones back. Requires interactive Clerk
    auth — an API key must not be able to destroy the account that minted it.
    """
    if auth.method != AuthMethod.CLERK_JWT:
        raise HTTPException(
            status_code=403,
            detail="Account deletion requires signing in (API keys not allowed)",
        )

    tombstoned: list[tuple[str, str]] = []
    async with get_session() as session:
        result = await session.execute(
            select(UserModel).where(UserModel.id == auth.user_id)
        )
        user = result.scalar_one_or_none()
        if not user or not user.clerk_user_id:
            raise HTTPException(status_code=404, detail="User not found")
        clerk_user_id = user.clerk_user_id

        rows_result = await session.execute(
            select(UserModel)
            .where(UserModel.clerk_user_id == clerk_user_id)
            .where(UserModel.is_active == True)  # noqa: E712
        )
        rows = list(rows_result.scalars().all())
        await _require_no_stranded_org(session, rows)

        for row in rows:
            row.is_active = False
            row.deleted_at = utcnow()
            tombstoned.append((row.org_id, row.id))
        await session.commit()

    # Drop this container's cached auth contexts as soon as the rows are
    # tombstoned, so a cached JWT context can't keep acting as a locally
    # deactivated user while the Clerk call below is in flight.
    invalidate_cached_clerk_auth(clerk_user_id)

    original_row_ids = [row_id for _, row_id in tombstoned]
    try:
        await _delete_clerk_user(clerk_user_id)
    except Exception:
        # Clerk still has the account, so undo the tombstones: the user keeps
        # a working account and can simply retry. A concurrent request with a
        # still-valid JWT may have JIT-provisioned fresh rows for this Clerk
        # user during the window — tombstone those first so the restore can't
        # leave duplicate active rows for one identity.
        async with get_session() as session:
            await session.execute(
                update(UserModel)
                .where(UserModel.clerk_user_id == clerk_user_id)
                .where(UserModel.id.notin_(original_row_ids))
                .where(UserModel.is_active == True)  # noqa: E712
                .values(is_active=False, deleted_at=utcnow())
            )
            await session.execute(
                update(UserModel)
                .where(UserModel.id.in_(original_row_ids))
                .values(is_active=True, deleted_at=None)
                .execution_options(include_deleted=True)
            )
            await session.commit()
        # Contexts cached during the window may point at the now-tombstoned
        # duplicate rows; drop them so the next request resolves the
        # restored originals.
        invalidate_cached_clerk_auth(clerk_user_id)
        raise

    # Point of no return: the Clerk account is gone. Sweep any rows a
    # concurrent JIT provisioning revived between the tombstone commit and
    # Clerk deletion, then drop contexts cached during that window. Other
    # containers' caches age out within the 60s TTL, Clerk revoked the
    # user's sessions above, and the ``user.deleted`` webhook is the
    # cross-container safety net that re-tombstones any later revival.
    async with get_session() as session:
        await session.execute(
            update(UserModel)
            .where(UserModel.clerk_user_id == clerk_user_id)
            .where(UserModel.is_active == True)  # noqa: E712
            .values(is_active=False, deleted_at=utcnow())
        )
        await session.commit()
    invalidate_cached_clerk_auth(clerk_user_id)

    # Tag ownership transfer runs best-effort per org, after the point of no
    # return. A failure must not surface as a deletion error (the Clerk
    # account is already gone); the hourly ``sweep_orphaned_tag_owners``
    # sweep is the documented safety net for any tags this leaves orphaned.
    for org_id, user_row_id in tombstoned:
        try:
            async with get_session() as session:
                await transfer_tag_ownership_to_admin(
                    session, org_id=org_id, deactivated_user_id=user_row_id
                )
                await session.commit()
        except Exception:
            logger.exception(
                "Account deletion: tag ownership transfer failed for user %s "
                "in org %s (tags left for the orphaned-owner sweep)",
                user_row_id,
                org_id,
            )

    return {"status": "deleted", "clerk_user_id": clerk_user_id}


@router.delete("/users/{user_id}")
async def remove_user(
    user_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Remove a user from the organization.

    Soft-deletes the row (stamps ``deleted_at`` and clears ``is_active``)
    so the session-level filter immediately hides the user from list /
    auth paths. ``is_active=False`` is preserved alongside the tombstone
    for any reader that hasn't migrated off the legacy flag.
    """

    async with get_session() as session:
        result = await session.execute(
            select(UserModel)
            .where(UserModel.id == user_id)
            .where(UserModel.org_id == auth.org_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Prevent removing the last admin. The admin count uses live rows
        # only -- the auto-filter already excludes soft-deleted users, so
        # the explicit ``is_active`` check just additionally ignores
        # deactivated-but-not-removed admins.
        if user.role == UserRole.ADMIN:
            admins = await session.execute(
                select(UserModel)
                .where(UserModel.org_id == auth.org_id)
                .where(UserModel.role == UserRole.ADMIN)
                .where(UserModel.is_active == True)  # noqa: E712
            )
            if len(list(admins.scalars().all())) <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot remove the last admin of the organization",
                )

        user.is_active = False
        user.deleted_at = utcnow()
        await transfer_tag_ownership_to_admin(
            session, org_id=auth.org_id, deactivated_user_id=user_id
        )
        await session.commit()

        return {"status": "removed", "user_id": user_id}
