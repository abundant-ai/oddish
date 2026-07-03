from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError

from api.schemas import (
    InviteUserRequest,
    InviteUserResponse,
    OrganizationResponse,
    OrgQuotaResponse,
    QuotaListResponse,
    QuotaMemberItem,
    QuotaUpdateRequest,
    QuotaUsageResponse,
    UserResponse,
)
from auth import (
    AuthContext,
    AuthMethod,
    require_admin,
    require_auth,
    require_can_manage_quotas,
)
from auth.verification import invalidate_cached_clerk_auth
from oddish.config import QuotaMode, settings
from models import OrgQuotaModel, QuotaModel, UserModel, UserRole, generate_id
from oddish.core.quotas import (
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    org_inflight_reserved_usd,
    start_of_today_utc,
    sum_cost_usd,
    sum_cost_usd_by_user,
    sum_org_cost_usd,
)
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


@router.get("/quotas/me", response_model=QuotaUsageResponse)
async def get_my_quota_usage(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QuotaUsageResponse:
    used_today = Decimal(0)
    reserved = Decimal(0)
    effective_limit_usd = settings.default_daily_quota_usd
    if auth.user_id:
        async with get_session() as session:
            used_today = await sum_cost_usd(
                session, auth.org_id, auth.user_id, start_of_today_utc()
            )
            effective_limit_usd = await get_effective_limit(
                session, auth.org_id, auth.user_id
            )
            reserved = await inflight_reserved_usd(
                session, auth.org_id, auth.user_id
            )
    return QuotaUsageResponse(
        user_id=auth.user_id or "",
        limit_usd=float(effective_limit_usd),
        used_usd=float(used_today),
        reserved_usd=float(reserved),
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
    )


def _as_float_or_none(value) -> float | None:
    return None if value is None else float(value)


async def _org_quota_fields(session, org_id, period_start) -> dict:
    """Effective org-wide cap, today's org-wide settled spend, in-flight
    reservation, and the configured default -- shared by GET /quotas and
    PUT /quotas/org so both agree."""
    effective_org_limit = await get_effective_org_limit(session, org_id)
    org_used = await sum_org_cost_usd(session, org_id, period_start)
    org_reserved = await org_inflight_reserved_usd(session, org_id)
    return {
        "org_limit_usd": _as_float_or_none(effective_org_limit),
        "org_used_usd": float(org_used),
        "org_reserved_usd": float(org_reserved),
        "org_default_limit_usd": _as_float_or_none(
            settings.default_org_daily_quota_usd
        ),
    }


async def _org_quota_fields_or_unavailable(org_id, period_start) -> dict:
    """GET /quotas resilience for the deploy-before-migrate window.

    The backend can boot before ``add_org_quotas_001`` has run: the startup
    guard forces ``quota_mode=off`` (protecting admissions) but does not take
    this read path down, so a missing ``org_quotas`` table must degrade to
    no-org-cap display fields instead of 500ing the whole admin quotas page.
    Runs in its OWN session so the aborted transaction cannot poison the
    caller's member query. PUT /quotas/org stays strict -- writing an org cap
    without the table is a real error.
    """
    try:
        async with get_session() as session:
            return await _org_quota_fields(session, org_id, period_start)
    except ProgrammingError:
        logger.warning(
            "GET /quotas org fields unavailable (org_quotas schema not "
            "migrated yet?); degrading to configured-default display fields",
            exc_info=True,
        )
        default = _as_float_or_none(settings.default_org_daily_quota_usd)
        return {
            "org_limit_usd": default,
            "org_used_usd": 0.0,
            "org_reserved_usd": 0.0,
            "org_default_limit_usd": default,
        }


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

        used_usd_by_user_id = await sum_cost_usd_by_user(
            session, auth.org_id, period_start
        )

        override_rows = await session.execute(
            select(QuotaModel.user_id, QuotaModel.limit_usd).where(
                QuotaModel.org_id == auth.org_id,
                QuotaModel.deleted_at.is_(None),
            )
        )
        override_limit_by_user_id = dict(override_rows.all())

    org_fields = await _org_quota_fields_or_unavailable(auth.org_id, period_start)

    return QuotaListResponse(
        members=[
            _quota_member_item(
                member,
                override_limit_by_user_id.get(member.id, default_limit_usd),
                used_usd_by_user_id.get(member.id, Decimal(0)),
            )
            for member in members
        ],
        **org_fields,
    )


@router.put("/quotas/org", response_model=OrgQuotaResponse)
async def set_org_quota(
    payload: QuotaUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> OrgQuotaResponse:
    """Set or clear the org-wide aggregate daily cap override.

    A null ``limit_usd`` clears the override (hard DELETE, like the per-user
    path) so the org reverts to the configured default. The effective cap is
    re-read after the write so PUT and GET agree even under a concurrent write.
    """
    async with get_session() as session:
        if payload.limit_usd is None:
            await session.execute(
                OrgQuotaModel.__table__.delete().where(
                    OrgQuotaModel.org_id == auth.org_id
                )
            )
        else:
            await session.execute(
                pg_insert(OrgQuotaModel)
                .values(
                    id=generate_id(),
                    org_id=auth.org_id,
                    limit_usd=payload.limit_usd,
                    period_kind="daily",
                )
                .on_conflict_do_update(
                    index_elements=["org_id"],
                    index_where=OrgQuotaModel.deleted_at.is_(None),
                    set_={
                        "limit_usd": payload.limit_usd,
                        "period_kind": "daily",
                        "updated_at": utcnow(),
                        "deleted_at": None,
                    },
                )
            )

        org_fields = await _org_quota_fields(
            session, auth.org_id, start_of_today_utc()
        )

    return OrgQuotaResponse(**org_fields)


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
                )
                .on_conflict_do_update(
                    index_elements=["org_id", "user_id"],
                    # Clear deleted_at too: get_effective_limit / the admin list
                    # only read live rows, so a PUT over a tombstoned override
                    # must revive it or the new limit would store but never
                    # enforce (the (org_id,user_id) unique index spans all rows).
                    set_={
                        "limit_usd": payload.limit_usd,
                        "updated_at": utcnow(),
                        "deleted_at": None,
                    },
                )
            )

        used_today = await sum_cost_usd(
            session, auth.org_id, user_id, start_of_today_utc()
        )
        effective_limit_usd = await get_effective_limit(session, auth.org_id, user_id)

    return _quota_member_item(member, effective_limit_usd, used_today)


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


async def _clerk_user_exists(clerk_user_id: str) -> bool | None:
    """Best-effort existence probe used when a Clerk delete errors.

    Returns True/False on a definitive answer, None when Clerk cannot be
    reached (or no secret is configured) — callers must treat None as
    "unknown", not as either definitive state.
    """
    if not CLERK_SECRET_KEY:
        return None

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return None


@router.delete("/users/me")
async def delete_my_account(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Delete the calling user's account.

    Soft-deletes the local user rows first (committed before Clerk is
    touched), then deletes the Clerk user. If the Clerk delete errors, the
    tombstones are rolled back only when Clerk confirms the account still
    exists; a confirmed-gone answer proceeds as success and an unknown answer
    keeps the tombstones and asks the user to retry. Every outcome is
    retryable and none leaves a Clerk-deleted identity active locally.
    Requires interactive Clerk auth — an API key must not be able to destroy
    the account that minted it.
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
        # Before undoing anything, check whether the Clerk account actually
        # survived: the delete may have succeeded before a timeout/network
        # error, or a parallel DELETE /users/me may have won. Restoring local
        # rows for a Clerk identity that no longer exists would let JWT
        # provisioning resurrect a usable account for a deleted sign-in, so
        # only a *confirmed-alive* answer restores; a definitive "gone"
        # proceeds as success, and "unknown" keeps the tombstones (the flow
        # is idempotent — retrying completes either way, and the
        # ``user.deleted`` webhook re-tombstones if deletion did land).
        clerk_alive = await _clerk_user_exists(clerk_user_id)
        if clerk_alive is True:
            # A concurrent request with a still-valid JWT may have
            # JIT-provisioned fresh rows during the window — tombstone those
            # first so the restore can't leave duplicate active rows.
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
            # Contexts cached during the window may point at the
            # now-tombstoned duplicate rows; drop them so the next request
            # resolves the restored originals.
            invalidate_cached_clerk_auth(clerk_user_id)
            raise
        if clerk_alive is None:
            invalidate_cached_clerk_auth(clerk_user_id)
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not confirm account deletion with the identity "
                    "provider. Please retry."
                ),
            )
        logger.warning(
            "Clerk delete for %s errored but the user is already gone; "
            "treating as success",
            clerk_user_id,
        )

    # Point of no return: the Clerk account is gone. Nothing past here may
    # fail the request — the UI must not report failure for a deletion that
    # already happened. Sweep any rows a concurrent JIT provisioning revived
    # between the tombstone commit and Clerk deletion, then drop contexts
    # cached during that window. Other containers' caches age out within the
    # 60s TTL, Clerk revoked the user's sessions above, and the
    # ``user.deleted`` webhook is the cross-container safety net that
    # re-tombstones any later revival.
    try:
        async with get_session() as session:
            await session.execute(
                update(UserModel)
                .where(UserModel.clerk_user_id == clerk_user_id)
                .where(UserModel.is_active == True)  # noqa: E712
                .values(is_active=False, deleted_at=utcnow())
            )
            await session.commit()
    except Exception:
        logger.exception(
            "Account deletion: post-Clerk revived-row sweep failed for %s "
            "(user.deleted webhook is the safety net)",
            clerk_user_id,
        )
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
