from __future__ import annotations

import logging
import os

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import OrganizationModel, UserModel, UserRole, generate_id

logger = logging.getLogger(__name__)

# Clerk secret key for API access
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")

# In preview Modal apps the seeded org is throwaway — let JIT-provisioned
# users land as ADMIN so they can manage users etc. Prod stays MEMBER.
_DEFAULT_JIT_ROLE = (
    UserRole.ADMIN
    if os.environ.get("MODAL_APP_NAME", "").startswith("oddish-pr-")
    else UserRole.MEMBER
)


def _github_account_from_clerk_payload(data: dict) -> tuple[str | None, str | None]:
    external_accounts = data.get("external_accounts") or []
    for account in external_accounts:
        if account.get("provider") != "oauth_github":
            continue
        username = account.get("username") or None
        email = (
            account.get("email_address")
            or account.get("email")
            or account.get("primary_email_address")
        )
        return username, email
    return None, None


async def fetch_github_identity_from_clerk(
    clerk_user_id: str,
) -> tuple[str | None, str | None]:
    """Return ``(github_username, github_email)`` from Clerk external accounts."""
    if not CLERK_SECRET_KEY:
        return None, None

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch Clerk user %s: %s", clerk_user_id, exc)
        return None, None

    return _github_account_from_clerk_payload(data)


async def fetch_github_username_from_clerk(clerk_user_id: str) -> str | None:
    username, _email = await fetch_github_identity_from_clerk(clerk_user_id)
    return username


def _seed_attribution_cache_from_github(
    user: UserModel,
    *,
    github_username: str | None,
    github_email: str | None,
) -> None:
    """Merge Clerk GitHub identity into the dashboard Mine alias cache."""
    raw = user.attribution_cache if isinstance(user.attribution_cache, dict) else {}
    handles: list[str] = [
        str(value).strip()
        for value in (raw.get("github_handles") or ())
        if str(value).strip()
    ]
    emails: list[str] = [
        str(value).strip()
        for value in (raw.get("legacy_emails") or ())
        if str(value).strip()
    ]
    seen_handles = {handle.lower() for handle in handles}
    seen_emails = {email.lower() for email in emails}

    def _add_handle(value: str | None) -> None:
        normalized = (value or "").strip().lstrip("@")
        if not normalized:
            return
        key = normalized.lower()
        if key in seen_handles:
            return
        seen_handles.add(key)
        handles.append(normalized)

    def _add_email(value: str | None) -> None:
        normalized = (value or "").strip()
        if not normalized or "@" not in normalized:
            return
        key = normalized.lower()
        if key in seen_emails:
            return
        seen_emails.add(key)
        emails.append(normalized)

    _add_handle(github_username)
    _add_email(user.email)
    _add_email(github_email)
    if not handles and not emails:
        return
    cache: dict[str, object] = {
        "github_handles": handles,
        "legacy_emails": emails,
    }
    prior_refreshed = raw.get("refreshed_at")
    if isinstance(prior_refreshed, str):
        # Preserve discovery timestamps; only _persist_profile sets a new one.
        cache["refreshed_at"] = prior_refreshed
    user.attribution_cache = cache


async def _refresh_user_github_identity(user: UserModel) -> None:
    if not user.clerk_user_id:
        return
    raw = user.attribution_cache if isinstance(user.attribution_cache, dict) else {}
    if user.github_username and isinstance(raw.get("refreshed_at"), str):
        return
    if user.github_username:
        _seed_attribution_cache_from_github(
            user,
            github_username=user.github_username,
            github_email=None,
        )
        return
    username, github_email = await fetch_github_identity_from_clerk(user.clerk_user_id)
    if username and not user.github_username:
        user.github_username = username
    if username or github_email:
        _seed_attribution_cache_from_github(
            user,
            github_username=username or user.github_username,
            github_email=github_email,
        )


async def ensure_user_github_identity(
    session: AsyncSession,
    user: UserModel,
) -> None:
    """Refresh ``github_username`` from Clerk when missing (one API call)."""
    if not user.clerk_user_id or user.github_username:
        return
    username, _email = await fetch_github_identity_from_clerk(user.clerk_user_id)
    if username:
        user.github_username = username
        await session.flush()


async def fetch_clerk_org_ids_for_user(clerk_user_id: str) -> list[str]:
    if not CLERK_SECRET_KEY:
        return []

    url = "https://api.clerk.com/v1/users/" f"{clerk_user_id}/organization_memberships"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning(
            "Failed to fetch Clerk org memberships for %s: %s", clerk_user_id, exc
        )
        return []

    memberships = data.get("data", data) if isinstance(data, dict) else data
    org_ids: list[str] = []
    if isinstance(memberships, list):
        for membership in memberships:
            if not isinstance(membership, dict):
                continue
            org = membership.get("organization") or {}
            org_id = (
                org.get("id")
                or membership.get("organization_id")
                or membership.get("organizationId")
            )
            if org_id:
                org_ids.append(org_id)
    return org_ids


async def get_org_from_clerk_id(
    session: AsyncSession, clerk_org_id: str
) -> OrganizationModel | None:
    org_result = await session.execute(
        select(OrganizationModel)
        .where(OrganizationModel.clerk_org_id == clerk_org_id)
        .where(OrganizationModel.is_active == True)  # noqa: E712
    )
    return org_result.scalar_one_or_none()


async def get_or_create_personal_org(
    session: AsyncSession, clerk_user_id: str
) -> OrganizationModel:
    org_slug = f"personal-{clerk_user_id}"
    slug_conflict = await session.execute(
        select(OrganizationModel)
        .where(OrganizationModel.slug == org_slug)
        .where(OrganizationModel.is_active == True)  # noqa: E712
    )
    org = slug_conflict.scalar_one_or_none()
    if org:
        return org

    org = OrganizationModel(
        id=generate_id(),
        name="Personal",
        slug=org_slug,
        clerk_org_id=None,
    )
    session.add(org)
    await session.flush()
    return org


def resolve_role(org_role: str | None, default_role: UserRole) -> UserRole:
    normalized_role = (org_role or "").lower()
    if normalized_role in {"owner", "org:owner", "admin", "org:admin"}:
        return UserRole.ADMIN
    if normalized_role in {"member", "org:member"}:
        return UserRole.MEMBER
    return default_role


async def get_or_create_user_in_org(
    session: AsyncSession,
    clerk_user_id: str,
    org: OrganizationModel,
    email: str | None,
    org_role: str | None,
    default_role: UserRole,
) -> UserModel:
    result = await session.execute(
        select(UserModel)
        .where(UserModel.clerk_user_id == clerk_user_id)
        .where(UserModel.org_id == org.id)
        .where(UserModel.is_active == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()
    if user:
        resolved_role = resolve_role(org_role, user.role)
        if resolved_role != user.role:
            user.role = resolved_role
        await _refresh_user_github_identity(user)
        return user

    if email:
        existing_email = await session.execute(
            select(UserModel)
            .where(UserModel.org_id == org.id)
            .where(UserModel.email == email)
            .where(UserModel.is_active == True)  # noqa: E712
        )
        existing_user = existing_email.scalar_one_or_none()
        if existing_user:
            existing_user.clerk_user_id = clerk_user_id
            resolved_role = resolve_role(org_role, existing_user.role)
            if resolved_role != existing_user.role:
                existing_user.role = resolved_role
            await _refresh_user_github_identity(existing_user)
            return existing_user

    role = resolve_role(org_role, default_role)
    user = UserModel(
        id=generate_id(),
        org_id=org.id,
        clerk_user_id=clerk_user_id,
        email=email or f"{clerk_user_id}@clerk.user",
        role=role,
    )
    session.add(user)
    await session.flush()

    await _refresh_user_github_identity(user)

    return user


async def get_or_create_user_from_clerk(
    session: AsyncSession,
    clerk_user_id: str,
    clerk_org_id: str | None,
    email: str | None,
    org_role: str | None,
) -> tuple[UserModel, OrganizationModel] | None:
    """
    Get or create a user from Clerk JWT claims.

    If the user doesn't exist and belongs to a Clerk org, we create the user.
    If no org is found locally, returns None (org must be provisioned first).
    """
    if clerk_org_id:
        org = await get_org_from_clerk_id(session, clerk_org_id)
        if not org:
            return None
        user = await get_or_create_user_in_org(
            session, clerk_user_id, org, email, org_role, _DEFAULT_JIT_ROLE
        )
        return user, org

    # User doesn't exist - try to resolve org when JWT is missing org_id
    if not clerk_org_id and email:
        existing_email = await session.execute(
            select(UserModel)
            .where(UserModel.email == email)
            .where(UserModel.is_active == True)  # noqa: E712
        )
        email_users = list(existing_email.scalars().all())
        if len(email_users) == 1:
            user = email_users[0]
            org_result = await session.execute(
                select(OrganizationModel)
                .where(OrganizationModel.id == user.org_id)
                .where(OrganizationModel.is_active == True)  # noqa: E712
            )
            org = org_result.scalar_one_or_none()
            if org:
                user.clerk_user_id = clerk_user_id
                await _refresh_user_github_identity(user)
                return user, org

    if not clerk_org_id:
        org_ids = await fetch_clerk_org_ids_for_user(clerk_user_id)
        if org_ids:
            org_result = await session.execute(
                select(OrganizationModel)
                .where(OrganizationModel.clerk_org_id.in_(org_ids))
                .where(OrganizationModel.is_active == True)  # noqa: E712
            )
            orgs = list(org_result.scalars().all())
            if len(orgs) == 1:
                clerk_org_id = orgs[0].clerk_org_id

    # If still no org, provision a personal org for the user
    if not clerk_org_id:
        org = await get_or_create_personal_org(session, clerk_user_id)
        user = await get_or_create_user_in_org(
            session, clerk_user_id, org, email, org_role, UserRole.ADMIN
        )
        return user, org

    org = await get_org_from_clerk_id(session, clerk_org_id)
    if not org:
        return None
    user = await get_or_create_user_in_org(
        session, clerk_user_id, org, email, org_role, _DEFAULT_JIT_ROLE
    )
    return user, org
