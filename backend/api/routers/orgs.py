from __future__ import annotations

import os
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.schemas import (
    InviteUserRequest,
    InviteUserResponse,
    OrganizationResponse,
    QuotaListResponse,
    QuotaMemberItem,
    QuotaUpdateRequest,
    QuotaUsageResponse,
    UserResponse,
)
from auth import (
    AuthContext,
    require_admin,
    require_auth,
    require_can_manage_quotas,
)
from oddish.config import QuotaMode, settings
from models import QuotaModel, UserModel, UserRole, generate_id
from oddish.core.quotas import (
    get_effective_limit,
    start_of_today_utc,
    sum_cost_usd,
    to_money_decimal,
)
from oddish.core.tags.ownership_transfer import transfer_tag_ownership_to_admin
from oddish.db import TrialModel, get_session, utcnow

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


@router.get("/quotas/me", response_model=QuotaUsageResponse)
async def get_my_quota_usage(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QuotaUsageResponse:
    used_today = Decimal(0)
    effective_limit_usd = settings.default_daily_quota_usd
    if auth.user_id:
        async with get_session() as session:
            used_today = await sum_cost_usd(
                session, auth.org_id, auth.user_id, start_of_today_utc()
            )
            effective_limit_usd = await get_effective_limit(
                session, auth.org_id, auth.user_id
            )
    return QuotaUsageResponse(
        user_id=auth.user_id or "",
        limit_usd=float(effective_limit_usd),
        used_usd=float(used_today),
        period="daily",
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
    )


def _quota_member_item(member, effective_limit_usd, used_usd) -> QuotaMemberItem:
    return QuotaMemberItem(
        user_id=member.id,
        email=member.email,
        name=member.name,
        github_username=member.github_username,
        role=member.role.value,
        limit_usd=float(effective_limit_usd),
        used_usd=float(used_usd),
        period="daily",
    )


@router.get("/quotas", response_model=QuotaListResponse)
async def list_member_quotas(
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaListResponse:
    period_start = start_of_today_utc()
    default_limit_usd = settings.default_daily_quota_usd

    async with get_session() as session:
        members = (
            (
                await session.execute(
                    select(UserModel)
                    .where(UserModel.org_id == auth.org_id)
                    .order_by(UserModel.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

        grouped_usage = await session.execute(
            select(
                TrialModel.billed_user_id,
                func.coalesce(func.sum(TrialModel.cost_usd), 0),
            )
            .where(
                TrialModel.org_id == auth.org_id,
                TrialModel.billed_user_id.is_not(None),
                TrialModel.finished_at >= period_start,
                TrialModel.deleted_at.is_(None),
            )
            .group_by(TrialModel.billed_user_id)
        )
        used_usd_by_user_id = {
            billed_user_id: to_money_decimal(settled_total)
            for billed_user_id, settled_total in grouped_usage.all()
        }

        override_rows = await session.execute(
            select(QuotaModel.user_id, QuotaModel.limit_usd).where(
                QuotaModel.org_id == auth.org_id,
                QuotaModel.deleted_at.is_(None),
            )
        )
        override_limit_by_user_id = dict(override_rows.all())

    return QuotaListResponse(
        members=[
            _quota_member_item(
                member,
                override_limit_by_user_id.get(member.id, default_limit_usd),
                used_usd_by_user_id.get(member.id, Decimal(0)),
            )
            for member in members
        ]
    )


@router.put("/quotas/{user_id}", response_model=QuotaMemberItem)
async def set_member_quota(
    user_id: str,
    payload: QuotaUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaMemberItem:
    async with get_session() as session:
        member = (
            await session.execute(
                select(UserModel).where(
                    UserModel.id == user_id, UserModel.org_id == auth.org_id
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise HTTPException(
                status_code=404, detail=f"User {user_id} not found in this org"
            )

        if payload.limit_usd is None:
            await session.execute(
                QuotaModel.__table__.delete().where(
                    QuotaModel.org_id == auth.org_id,
                    QuotaModel.user_id == user_id,
                )
            )
        else:
            await session.execute(
                pg_insert(QuotaModel)
                .values(
                    id=generate_id(),
                    org_id=auth.org_id,
                    user_id=user_id,
                    limit_usd=payload.limit_usd,
                    period_kind="daily",
                )
                .on_conflict_do_update(
                    index_elements=["org_id", "user_id"],
                    set_={"limit_usd": payload.limit_usd, "updated_at": utcnow()},
                )
            )

        used_today = await sum_cost_usd(
            session, auth.org_id, user_id, start_of_today_utc()
        )

    return _quota_member_item(
        member,
        payload.limit_usd
        if payload.limit_usd is not None
        else settings.default_daily_quota_usd,
        used_today,
    )


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
