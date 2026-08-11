from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import OrganizationModel, UserModel, UserRole, generate_id
from oddish.timing import timed_phase

logger = logging.getLogger(__name__)

# Clerk secret key for API access
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")

# A checked-absent marker older than this is treated as UNchecked everywhere so a
# user who links GitHub after being stamped self-heals on the next refresh/backfill.
GITHUB_ID_RECHECK_TTL = timedelta(hours=1)


def github_id_recheck_cutoff(now: datetime | None = None) -> datetime:
    reference = now or datetime.now(timezone.utc)
    return reference - GITHUB_ID_RECHECK_TTL


def _marker_is_fresh(checked_at: datetime | None, now: datetime | None = None) -> bool:
    if checked_at is None:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return checked_at > github_id_recheck_cutoff(now)


# In preview Modal apps the seeded org is throwaway — let JIT-provisioned
# users land as ADMIN so they can manage users etc. Prod stays MEMBER.
_DEFAULT_JIT_ROLE = (
    UserRole.ADMIN
    if os.environ.get("MODAL_APP_NAME", "").startswith("oddish-pr-")
    else UserRole.MEMBER
)


@dataclass(frozen=True)
class ClerkGithubIdentity:
    username: str | None
    email: str | None
    github_id: str | None


def _github_account_from_clerk_payload(data: dict) -> ClerkGithubIdentity:
    external_accounts = data.get("external_accounts") or []
    for account in external_accounts:
        if account.get("provider") != "oauth_github":
            continue
        return ClerkGithubIdentity(
            username=account.get("username") or None,
            email=(
                account.get("email_address")
                or account.get("email")
                or account.get("primary_email_address")
            ),
            github_id=account.get("provider_user_id") or None,
        )
    return ClerkGithubIdentity(None, None, None)


async def _fetch_clerk_user_payload(clerk_user_id: str) -> dict | None:
    if not CLERK_SECRET_KEY:
        return None

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            with timed_phase("external_http", service="clerk", operation="user"):
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        # Any HTTP failure — including 404 — is non-definitive. A 404 is
        # indistinguishable from a misconfigured CLERK_SECRET_KEY (wrong Clerk
        # instance 404s for EVERY user), so callers must treat None as "unknown",
        # never as a settled "no linked account": doing otherwise would let a
        # misconfig silently mass-unlink existing identities. Genuinely deleted
        # Clerk users are soft-deleted via the membership webhook, so retrying
        # them here is a bounded cost.
        logger.warning("Failed to fetch Clerk user %s: %s", clerk_user_id, exc)
        return None


async def fetch_github_identity_from_clerk(
    clerk_user_id: str,
) -> ClerkGithubIdentity | None:
    data = await _fetch_clerk_user_payload(clerk_user_id)
    if data is None:
        return None
    return _github_account_from_clerk_payload(data)


def _slack_user_id_from_clerk_payload(data: dict) -> str | None:
    for account in data.get("external_accounts") or []:
        if account.get("provider") != "oauth_slack":
            continue
        # Clerk stores the Slack account's provider_user_id from Slack's OpenID
        # Connect. Depending on the connection it is either the bare Slack user
        # id (U…/W…) or a "<team_id>-<user_id>" composite (T…-U…). Slack user
        # ids start with U (W for enterprise grid), team ids with T, so take the
        # user-id segment; anything unrecognized returns None so the caller
        # falls back to the email-based Slack lookup.
        for segment in (account.get("provider_user_id") or "").split("-"):
            if segment[:1] in ("U", "W"):
                return segment
    return None


async def fetch_slack_user_id_from_clerk(clerk_user_id: str) -> str | None:
    data = await _fetch_clerk_user_payload(clerk_user_id)
    if data is None:
        return None
    return _slack_user_id_from_clerk_payload(data)


async def fetch_github_username_from_clerk(clerk_user_id: str) -> str | None:
    identity = await fetch_github_identity_from_clerk(clerk_user_id)
    return identity.username if identity else None


async def _set_github_id_if_absent(
    session: AsyncSession | None, user: UserModel, github_id: str | None
) -> None:
    if not github_id or user.github_id:
        return
    if session is not None:
        # Match uq_users_org_github_id scope, including soft-deleted rows.
        clash = await session.execute(
            select(UserModel)
            .where(UserModel.org_id == user.org_id)
            .where(UserModel.github_id == github_id)
            .where(UserModel.id != user.id)
            .execution_options(include_deleted=True)
        )
        for other in clash.scalars().all():
            if other.deleted_at is None and other.is_active:
                logger.warning(
                    "Skipping github_id %s for user %s: already claimed in org %s",
                    github_id,
                    user.id,
                    user.org_id,
                )
                return
            # Soft-deleted / deactivated holder: release the id so a rejoining
            # user can relink instead of being gated forever.
            other.github_id = None
        await session.flush()
        # Claim inside a SAVEPOINT: the assignment + flush must live under the
        # savepoint so a concurrent claim that raced past the clash query trips
        # uq_users_org_github_id here instead of poisoning the whole transaction.
        try:
            async with session.begin_nested():
                user.github_id = github_id
                await session.flush()
        except IntegrityError:
            user.github_id = None
            logger.warning(
                "Lost concurrent race claiming github_id %s for user %s in org %s",
                github_id,
                user.id,
                user.org_id,
            )
        return
    user.github_id = github_id


async def _apply_github_id(
    session: AsyncSession | None, user: UserModel, github_id: str | None
) -> None:
    """Reconcile ``user.github_id`` toward an authoritative Clerk id.

    Unlike ``_set_github_id_if_absent`` (first-write only), this also relinks a
    changed id: a truthy id that differs from the stored one drops the stale
    value before reclaiming it through the same clash / savepoint machinery, so
    the gate never keeps trusting an id Clerk no longer reports for this user.
    Clearing on a definitive no-github answer is the caller's job.
    """
    if not github_id or user.github_id == github_id:
        return
    user.github_id = None
    await _set_github_id_if_absent(session, user, github_id)


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


def _mark_github_id_checked(user: UserModel) -> None:
    user.github_id_checked_at = datetime.now(timezone.utc)


async def _refresh_user_github_identity(
    user: UserModel, session: AsyncSession | None = None
) -> None:
    if not user.clerk_user_id:
        return
    raw = user.attribution_cache if isinstance(user.attribution_cache, dict) else {}
    github_id_known = bool(user.github_id) or _marker_is_fresh(
        user.github_id_checked_at
    )
    if github_id_known:
        if user.github_username:
            if not isinstance(raw.get("refreshed_at"), str):
                _seed_attribution_cache_from_github(
                    user,
                    github_username=user.github_username,
                    github_email=None,
                )
            return
        if not user.github_id:
            return
    identity = await fetch_github_identity_from_clerk(user.clerk_user_id)
    if identity is None:
        return
    if identity.username and not user.github_username:
        user.github_username = identity.username
    await _apply_github_id(session, user, identity.github_id)
    if identity.username or identity.email:
        _seed_attribution_cache_from_github(
            user,
            github_username=identity.username or user.github_username,
            github_email=identity.email,
        )
    # Only a definitive no-github answer stamps the marker. If Clerk returned a
    # github_id we couldn't claim (active clash / lost race), or reported a
    # username with the id still absent (partial answer), leave it unstamped so
    # the backfill/refresh retries once the id is claimable.
    if not identity.github_id and not identity.username:
        # Clerk unlinked GitHub: drop any stale id so the gate stops trusting it.
        user.github_id = None
        _mark_github_id_checked(user)


async def ensure_user_github_identity(
    session: AsyncSession,
    user: UserModel,
) -> None:
    if not user.clerk_user_id or user.github_username:
        return
    identity = await fetch_github_identity_from_clerk(user.clerk_user_id)
    if identity and identity.username:
        user.github_username = identity.username
        await _set_github_id_if_absent(session, user, identity.github_id)
        await session.flush()


async def fetch_clerk_org_ids_for_user(clerk_user_id: str) -> list[str]:
    if not CLERK_SECRET_KEY:
        return []

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}/organization_memberships"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            with timed_phase(
                "external_http", service="clerk", operation="memberships"
            ):
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
        await _refresh_user_github_identity(user, session)
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
            await _refresh_user_github_identity(existing_user, session)
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

    await _refresh_user_github_identity(user, session)

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

    # JWT is missing org_id (classic CLERK_JWT_TEMPLATE misconfig). Adopt a
    # unique existing membership; refuse to invent/pick a tenant when several
    # match. A genuine zero-org user still gets a personal org below.
    if not clerk_org_id and email:
        # Joined to the org and filtered on ``is_active`` so a membership in a
        # DEACTIVATED org neither gets adopted nor counts toward ambiguity --
        # otherwise a user with a live row in a dead org plus one real org looks
        # ambiguous here and is refused, even though their tenant is unique.
        existing_email = await session.execute(
            select(UserModel, OrganizationModel)
            .join(OrganizationModel, OrganizationModel.id == UserModel.org_id)
            .where(UserModel.email == email)
            .where(UserModel.is_active == True)  # noqa: E712
            .where(OrganizationModel.is_active == True)  # noqa: E712
        )
        email_matches = list(existing_email.all())
        if len(email_matches) == 1:
            user, org = email_matches[0]
            user.clerk_user_id = clerk_user_id
            await _refresh_user_github_identity(user, session)
            return user, org
        if len(email_matches) > 1:
            logger.error(
                "Ambiguous org for clerk_user_id=%s: session token is missing "
                "org_id claim and %d active orgs match this email; "
                "CLERK_JWT_TEMPLATE is likely misconfigured",
                clerk_user_id,
                len(email_matches),
            )
            return None

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
            elif len(orgs) > 1:
                logger.error(
                    "Ambiguous org for clerk_user_id=%s: session token is missing "
                    "org_id claim and %d provisioned orgs match; "
                    "CLERK_JWT_TEMPLATE is likely misconfigured",
                    clerk_user_id,
                    len(orgs),
                )
                return None

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
