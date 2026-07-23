from __future__ import annotations

import calendar
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError

from api.schemas import (
    InviteUserRequest,
    InviteUserResponse,
    OrganizationResponse,
    PreTrialAnalysisSettingResponse,
    PreTrialAnalysisSettingUpdate,
    OrgQuotaResponse,
    OrgUsageResponse,
    QuotaBumpRequest,
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
from pg_errors import is_undefined_table_error
from oddish.config import QuotaMode, settings
from models import (
    OrganizationModel,
    OrgQuotaModel,
    QuotaBumpModel,
    QuotaModel,
    UserModel,
    UserRole,
    generate_id,
)
from oddish.core.quotas import (
    get_base_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    live_bump_total,
    live_bump_totals_by_user,
    org_inflight_reserved_usd,
    quota_window_start,
    start_of_month_utc,
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

PRE_TRIAL_ANALYSIS_SETTING = "pre_trial_analysis_enabled"


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


@router.get(
    "/org/settings/pre-trial-analysis",
    response_model=PreTrialAnalysisSettingResponse,
)
async def get_pre_trial_analysis_setting(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PreTrialAnalysisSettingResponse:
    """Return the effective org setting; the deployment flag is its default."""
    org_settings = auth.org.settings if auth.org and auth.org.settings else {}
    enabled = org_settings.get(PRE_TRIAL_ANALYSIS_SETTING)
    if not isinstance(enabled, bool):
        enabled = settings.pre_trial_enabled
    return PreTrialAnalysisSettingResponse(
        enabled=enabled,
        can_manage=auth.user_role == UserRole.ADMIN,
    )


@router.put(
    "/org/settings/pre-trial-analysis",
    response_model=PreTrialAnalysisSettingResponse,
)
async def update_pre_trial_analysis_setting(
    data: PreTrialAnalysisSettingUpdate,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> PreTrialAnalysisSettingResponse:
    """Enable or disable pre-trial QA for this organization."""
    async with get_session() as session:
        org = await session.get(OrganizationModel, auth.org_id, with_for_update=True)
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        org.settings = {
            **(org.settings or {}),
            PRE_TRIAL_ANALYSIS_SETTING: data.enabled,
        }
        await session.commit()
    return PreTrialAnalysisSettingResponse(enabled=data.enabled, can_manage=True)


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
    used_usd = reserved = bump_usd = Decimal(0)
    base_limit_usd, bump_expires_at = settings.default_daily_quota_usd, None
    if auth.user_id:
        async with get_session() as session:
            used_usd, base_limit_usd, bump_usd, bump_expires_at = (
                await _read_member_quota_fields(session, auth.org_id, auth.user_id)
            )
            reserved = await inflight_reserved_usd(session, auth.org_id, auth.user_id)

    return QuotaUsageResponse(
        user_id=auth.user_id or "",
        limit_usd=float(base_limit_usd + bump_usd),
        used_usd=float(used_usd),
        reserved_usd=float(reserved),
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
        base_limit_usd=float(base_limit_usd),
        bump_usd=float(bump_usd),
        bump_expires_at=bump_expires_at.isoformat() if bump_expires_at else None,
    )


async def _get_member_or_404(session, org_id: str | None, user_id: str) -> UserModel:
    member = (
        await session.execute(
            select(UserModel).where(
                UserModel.id == user_id, UserModel.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=404, detail=f"User {user_id} not found in this org"
        )
    return member


async def _read_member_quota_fields(session, org_id: str | None, user_id: str):
    used_usd = await sum_cost_usd(session, org_id, user_id, quota_window_start())
    base_limit_usd = await get_base_limit(session, org_id, user_id)
    bump_usd, bump_expires_at = await _bump_total_or_zero(session, org_id, user_id)
    return used_usd, base_limit_usd, bump_usd, bump_expires_at


def _quota_member_item(
    member, used_usd, base_limit_usd, bump_usd=Decimal(0), bump_expires_at=None
) -> QuotaMemberItem:
    return QuotaMemberItem(
        user_id=member.id,
        email=member.email,
        name=member.name,
        github_username=member.github_username,
        role=member.role.value,
        limit_usd=float(base_limit_usd + bump_usd),
        used_usd=float(used_usd),
        base_limit_usd=float(base_limit_usd),
        bump_usd=float(bump_usd),
        bump_expires_at=bump_expires_at.isoformat() if bump_expires_at else None,
    )


async def _member_quota_item(session, org_id: str | None, member) -> QuotaMemberItem:
    fields = await _read_member_quota_fields(session, org_id, member.id)
    return _quota_member_item(member, *fields)


def _as_float_or_none(value) -> float | None:
    return None if value is None else float(value)


async def _org_trial_usage(session, org_id) -> tuple[Decimal, Decimal]:
    used = await sum_org_cost_usd(session, org_id, start_of_month_utc())
    reserved = await org_inflight_reserved_usd(session, org_id)
    return used, reserved


async def _org_quota_fields(session, org_id) -> dict:
    effective_org_limit = await get_effective_org_limit(session, org_id)
    org_used, org_reserved = await _org_trial_usage(session, org_id)
    return {
        "org_limit_usd": _as_float_or_none(effective_org_limit),
        "org_used_usd": float(org_used),
        "org_reserved_usd": float(org_reserved),
        "org_default_limit_usd": _as_float_or_none(
            settings.default_org_monthly_quota_usd
        ),
    }


async def _org_quota_fields_no_cap_table(org_id) -> dict:
    # org_quotas is missing (deploy-before-migrate): the cap falls back to the
    # configured default, but month spend + in-flight reservation live on the
    # trials table and stay readable, so report the real usage, not zero.
    default = _as_float_or_none(settings.default_org_monthly_quota_usd)
    async with get_session() as session:
        org_used, org_reserved = await _org_trial_usage(session, org_id)
    return {
        "org_limit_usd": default,
        "org_used_usd": float(org_used),
        "org_reserved_usd": float(org_reserved),
        "org_default_limit_usd": default,
    }


async def _org_quota_fields_or_unavailable(org_id) -> dict:
    try:
        async with get_session() as session:
            return await _org_quota_fields(session, org_id)
    except ProgrammingError as exc:
        if not is_undefined_table_error(exc):
            raise
        logger.warning(
            "GET /quotas org cap unavailable (org_quotas schema not "
            "migrated yet); degrading limit to default, usage still real",
            exc_info=True,
        )
        return await _org_quota_fields_no_cap_table(org_id)


async def _bump_totals_or_empty(org_id) -> dict:
    # quota_bumps can be missing in a deploy-before-migrate window; the boost
    # feature then reads as "no live boosts" until the migration lands, mirroring
    # the org-cap fallback above. Own session so a missing table cannot poison a
    # caller's transaction.
    try:
        async with get_session() as session:
            return await live_bump_totals_by_user(session, org_id)
    except ProgrammingError as exc:
        if not is_undefined_table_error(exc):
            raise
        logger.warning(
            "boosts unavailable (quota_bumps schema not migrated yet); "
            "treating members as un-boosted",
            exc_info=True,
        )
        return {}


async def _bump_total_or_zero(
    session, org_id, user_id
) -> tuple[Decimal, datetime | None]:
    # Savepoint so a missing quota_bumps table (deploy-before-migrate) rolls back
    # just this read and leaves the caller's transaction usable -- while still
    # seeing the caller's own uncommitted writes, so POST/DELETE can build their
    # response from a re-read in the same transaction.
    try:
        async with session.begin_nested():
            return await live_bump_total(session, org_id, user_id)
    except ProgrammingError as exc:
        if not is_undefined_table_error(exc):
            raise
        logger.warning(
            "boost lookup unavailable (quota_bumps schema not migrated yet); "
            "treating member as un-boosted",
            exc_info=True,
        )
        return Decimal(0), None


@router.get("/quotas", response_model=QuotaListResponse)
async def list_member_quotas(
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaListResponse:
    period_start = quota_window_start()
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

    bump_totals_by_user_id = await _bump_totals_or_empty(auth.org_id)
    org_fields = await _org_quota_fields_or_unavailable(auth.org_id)

    return QuotaListResponse(
        members=[
            _quota_member_item(
                member,
                used_usd_by_user_id.get(member.id, Decimal(0)),
                override_limit_by_user_id.get(member.id, default_limit_usd),
                *bump_totals_by_user_id.get(member.id, (Decimal(0), None)),
            )
            for member in members
        ],
        **org_fields,
    )


def _org_usage_response(org_fields: dict, org_used_today: Decimal) -> OrgUsageResponse:
    now = start_of_today_utc()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_remaining = days_in_month - now.day + 1

    limit = org_fields["org_limit_usd"]
    used_month = Decimal(str(org_fields["org_used_usd"]))
    reserved = Decimal(str(org_fields["org_reserved_usd"]))
    daily_goal: float | None = None
    if limit is not None:
        remaining_at_day_start = max(
            Decimal(0),
            Decimal(str(limit)) - used_month + org_used_today - reserved,
        )
        daily_goal = float(remaining_at_day_start / Decimal(days_remaining))

    return OrgUsageResponse(
        org_limit_usd=limit,
        org_used_month_usd=float(used_month),
        org_reserved_usd=org_fields["org_reserved_usd"],
        org_used_today_usd=float(org_used_today),
        daily_goal_usd=daily_goal,
        days_remaining=days_remaining,
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
    )


@router.get("/quotas/org", response_model=OrgUsageResponse)
async def get_org_quota_usage(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> OrgUsageResponse:
    if auth.org_id is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        async with get_session() as session:
            org_fields = await _org_quota_fields(session, auth.org_id)
            org_used_today = await sum_org_cost_usd(
                session, auth.org_id, start_of_today_utc()
            )
    except ProgrammingError as exc:
        if not is_undefined_table_error(exc):
            raise
        logger.warning(
            "GET /quotas/org cap unavailable (org_quotas schema not migrated "
            "yet); degrading limit to default, usage still real",
            exc_info=True,
        )
        org_fields = await _org_quota_fields_no_cap_table(auth.org_id)
        async with get_session() as session:
            org_used_today = await sum_org_cost_usd(
                session, auth.org_id, start_of_today_utc()
            )

    return _org_usage_response(org_fields, org_used_today)


@router.put("/quotas/org", response_model=OrgQuotaResponse)
async def set_org_quota(
    payload: QuotaUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> OrgQuotaResponse:
    try:
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
                        period_kind="monthly",
                    )
                    .on_conflict_do_update(
                        index_elements=["org_id"],
                        index_where=OrgQuotaModel.deleted_at.is_(None),
                        # Clear deleted_at too: get_effective_org_limit / the
                        # admin display only read live rows, so a PUT over a
                        # tombstoned override must revive it (the partial unique
                        # index only spans live rows).
                        set_={
                            "limit_usd": payload.limit_usd,
                            "period_kind": "monthly",
                            "updated_at": utcnow(),
                            "deleted_at": None,
                        },
                    )
                )

            org_fields = await _org_quota_fields(session, auth.org_id)
    except ProgrammingError as exc:
        if not is_undefined_table_error(exc):
            raise
        raise HTTPException(
            status_code=503,
            detail=(
                "Org quotas are not available yet (schema is still migrating). "
                "Try again shortly."
            ),
        ) from exc

    return OrgQuotaResponse(**org_fields)


@router.put("/quotas/{user_id}", response_model=QuotaMemberItem)
async def set_member_quota(
    user_id: str,
    payload: QuotaUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaMemberItem:
    async with get_session() as session:
        member = await _get_member_or_404(session, auth.org_id, user_id)

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
                    set_={
                        "limit_usd": payload.limit_usd,
                        "updated_at": utcnow(),
                        "deleted_at": None,
                    },
                )
            )

        return await _member_quota_item(session, auth.org_id, member)


@router.post("/quotas/{user_id}/bumps", response_model=QuotaMemberItem)
async def add_member_quota_bump(
    user_id: str,
    payload: QuotaBumpRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaMemberItem:
    async with get_session() as session:
        member = await _get_member_or_404(session, auth.org_id, user_id)

        db_now = await session.scalar(sa_text("SELECT NOW()"))
        session.add(
            QuotaBumpModel(
                id=generate_id(),
                org_id=auth.org_id,
                user_id=user_id,
                amount_usd=payload.amount_usd,
                expires_at=db_now + timedelta(hours=payload.duration_hours),
                reason=payload.reason,
                granted_by_user_id=auth.user_id,
            )
        )
        await session.flush()
        return await _member_quota_item(session, auth.org_id, member)


@router.delete("/quotas/{user_id}/bumps", response_model=QuotaMemberItem)
async def revoke_member_quota_bumps(
    user_id: str,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaMemberItem:
    async with get_session() as session:
        member = await _get_member_or_404(session, auth.org_id, user_id)

        await session.execute(
            update(QuotaBumpModel)
            .where(
                QuotaBumpModel.org_id == auth.org_id,
                QuotaBumpModel.user_id == user_id,
                QuotaBumpModel.revoked_at.is_(None),
                QuotaBumpModel.deleted_at.is_(None),
                QuotaBumpModel.expires_at > func.now(),
            )
            .values(revoked_at=utcnow(), updated_at=utcnow())
        )
        await session.flush()
        return await _member_quota_item(session, auth.org_id, member)


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
            await session.execute(
                select(UserModel.id, UserModel.role)
                .where(UserModel.org_id == row.org_id)
                .where(UserModel.id != row.id)
                .where(UserModel.is_active == True)  # noqa: E712
            )
        ).all()
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
    except httpx.HTTPError as exc:
        logger.warning(
            "Clerk existence probe for %s failed (treating as unknown): %s",
            clerk_user_id,
            exc,
        )
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
    except Exception as delete_exc:
        logger.warning(
            "Account deletion: Clerk delete for %s failed: %s",
            clerk_user_id,
            delete_exc,
        )
        # Check whether the Clerk account actually survived: the delete may
        # have succeeded before a timeout/network error, or a parallel
        # DELETE /users/me may have won.
        clerk_alive = await _clerk_user_exists(clerk_user_id)
        if clerk_alive is False:
            logger.warning(
                "Clerk delete for %s errored but the user is already gone; "
                "treating as success",
                clerk_user_id,
            )
        else:
            # Confirmed alive OR unknown: restore the tombstones and surface
            # the real error. Keeping tombstones on "unknown" would brick the
            # account whenever Clerk is persistently unreachable or the
            # secret is misconfigured (both calls fail identically): locally
            # deactivated, Clerk sign-in still alive. The opposite mistake —
            # restoring rows for an identity whose deletion actually landed —
            # is recoverable: the sign-in is gone, no new JWT can use the
            # rows, and the ``user.deleted`` webhook re-tombstones them.
            #
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
            # Propagate the underlying failure so the dialog shows an
            # actionable reason instead of a generic retry message.
            if isinstance(delete_exc, HTTPException):
                raise delete_exc
            raise HTTPException(
                status_code=503,
                detail=f"Could not delete your sign-in account: {delete_exc}",
            ) from delete_exc

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
