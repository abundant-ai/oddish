from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.provisioning import (
    ensure_user_github_identity,
    fetch_github_identity_from_clerk,
)
from auth.types import AuthContext
from models import APIKeyModel, UserModel
from oddish.core.dashboard import UNRESOLVED_EXPERIMENTS_OWNER
from oddish.db import TaskModel

# Re-export for router imports.
__all__ = [
    "AttributionProfile",
    "resolve_experiments_author",
    "invalidate_attribution_cache",
]

_MEMORY_TTL_SECONDS = 15 * 60
_DB_TTL_SECONDS = 24 * 60 * 60

_memory_cache: dict[str, tuple[AttributionProfile, float]] = {}


@dataclass(frozen=True, slots=True)
class AttributionProfile:
    github_handles: tuple[str, ...]
    legacy_emails: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "github_handles": list(self.github_handles),
            "legacy_emails": list(self.legacy_emails),
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> AttributionProfile | None:
        if not raw:
            return None
        handles = tuple(
            str(value).strip()
            for value in (raw.get("github_handles") or ())
            if str(value).strip()
        )
        emails = tuple(
            str(value).strip()
            for value in (raw.get("legacy_emails") or ())
            if str(value).strip()
        )
        if not handles and not emails:
            return None
        return cls(github_handles=handles, legacy_emails=emails)


def _normalize_github_handle(value: str | None) -> str | None:
    normalized = (value or "").strip().lstrip("@")
    return normalized or None


def _looks_like_email(value: str) -> bool:
    return "@" in value


def _cache_key(org_id: str, user_id: str) -> str:
    return f"{org_id}:{user_id}"


def _memory_get(org_id: str, user_id: str) -> AttributionProfile | None:
    entry = _memory_cache.get(_cache_key(org_id, user_id))
    if entry is None:
        return None
    profile, cached_at = entry
    if time.time() - cached_at > _MEMORY_TTL_SECONDS:
        _memory_cache.pop(_cache_key(org_id, user_id), None)
        return None
    return profile


def _memory_set(org_id: str, user_id: str, profile: AttributionProfile) -> None:
    _memory_cache[_cache_key(org_id, user_id)] = (profile, time.time())


def invalidate_attribution_cache(*, org_id: str, user_id: str) -> None:
    _memory_cache.pop(_cache_key(org_id, user_id), None)


def _db_cache_fresh(user: UserModel) -> AttributionProfile | None:
    raw = user.attribution_cache if isinstance(user.attribution_cache, dict) else None
    if not raw:
        return None
    refreshed_at = raw.get("refreshed_at")
    if not isinstance(refreshed_at, str):
        return None
    try:
        refreshed = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - refreshed).total_seconds()
    if age > _DB_TTL_SECONDS:
        return None
    return AttributionProfile.from_dict(raw)


async def _other_member_github_handles(
    session: AsyncSession,
    *,
    org_id: str,
    exclude_user_id: str,
) -> set[str]:
    rows = await session.execute(
        select(UserModel.github_username)
        .where(UserModel.org_id == org_id)
        .where(UserModel.id != exclude_user_id)
        .where(UserModel.is_active == True)  # noqa: E712
        .where(UserModel.github_username.isnot(None))
    )
    blocked: set[str] = set()
    for (handle,) in rows:
        normalized = _normalize_github_handle(handle)
        if normalized:
            blocked.add(normalized.lower())
    return blocked


async def _other_member_emails(
    session: AsyncSession,
    *,
    org_id: str,
    exclude_user_id: str,
) -> set[str]:
    rows = await session.execute(
        select(UserModel.email)
        .where(UserModel.org_id == org_id)
        .where(UserModel.id != exclude_user_id)
        .where(UserModel.is_active == True)  # noqa: E712
        .where(UserModel.email.isnot(None))
    )
    blocked: set[str] = set()
    for (email,) in rows:
        normalized = (email or "").strip().lower()
        if normalized:
            blocked.add(normalized)
    return blocked


def _baseline_profile(
    user: UserModel,
    *,
    blocked_handles: set[str],
    blocked_emails: set[str],
    github_email: str | None = None,
) -> AttributionProfile:
    handles: list[str] = []
    emails: list[str] = []
    seen_handles: set[str] = set()
    seen_emails: set[str] = set()

    def _add_handle(raw: str | None) -> None:
        normalized = _normalize_github_handle(raw)
        if not normalized or normalized.lower() in blocked_handles:
            return
        key = normalized.lower()
        if key in seen_handles:
            return
        seen_handles.add(key)
        handles.append(normalized)

    def _add_email(raw: str | None) -> None:
        value = (raw or "").strip()
        if not value or not _looks_like_email(value):
            return
        key = value.lower()
        if key in seen_emails or key in blocked_emails:
            return
        seen_emails.add(key)
        emails.append(value)

    _add_handle(user.github_username)
    _add_email(user.email)
    _add_email(github_email)
    return AttributionProfile(
        github_handles=tuple(handles),
        legacy_emails=tuple(emails),
    )


async def _discover_attribution_from_tasks(
    session: AsyncSession,
    user: UserModel,
    *,
    org_id: str,
    baseline: AttributionProfile,
    blocked_handles: set[str],
    blocked_emails: set[str],
) -> AttributionProfile:
    """Scan the user's attributed tasks once to learn legacy handles/emails."""
    handles = list(baseline.github_handles)
    emails = list(baseline.legacy_emails)
    seen_handles = {handle.lower() for handle in handles}
    seen_emails = {email.lower() for email in emails}

    def _add_handle(raw: str | None) -> None:
        normalized = _normalize_github_handle(raw)
        if not normalized or normalized.lower() in blocked_handles:
            return
        key = normalized.lower()
        if key in seen_handles:
            return
        seen_handles.add(key)
        handles.append(normalized)

    def _add_email(raw: str | None) -> None:
        value = (raw or "").strip()
        if not value or not _looks_like_email(value):
            return
        key = value.lower()
        if key in seen_emails or key in blocked_emails:
            return
        seen_emails.add(key)
        emails.append(value)

    tag_expr = TaskModel.tags["github_username"].astext
    attribution_predicates = [TaskModel.created_by_user_id == user.id]
    if user.email:
        attribution_predicates.append(TaskModel.user == user.email)
    if baseline.github_handles:
        if len(baseline.github_handles) == 1:
            attribution_predicates.append(tag_expr == baseline.github_handles[0])
            attribution_predicates.append(
                TaskModel.user == baseline.github_handles[0]
            )
        else:
            attribution_predicates.append(tag_expr.in_(baseline.github_handles))
            attribution_predicates.append(
                TaskModel.user.in_(baseline.github_handles)
            )

    rows = await session.execute(
        select(tag_expr, TaskModel.user)
        .where(TaskModel.org_id == org_id)
        .where(TaskModel.deleted_at.is_(None))
        .where(or_(*attribution_predicates))
        .distinct()
        .limit(200)
    )
    for tag, raw_user in rows:
        _add_handle(tag)
        if raw_user and not _looks_like_email(raw_user):
            _add_handle(raw_user)
        else:
            _add_email(raw_user)

    return AttributionProfile(
        github_handles=tuple(handles),
        legacy_emails=tuple(emails),
    )


async def _persist_profile(
    session: AsyncSession,
    user: UserModel,
    profile: AttributionProfile,
) -> None:
    user.attribution_cache = profile.as_dict()
    _memory_set(user.org_id, user.id, profile)


async def _load_attribution_profile(
    session: AsyncSession,
    user: UserModel,
    *,
    org_id: str,
) -> AttributionProfile:
    cached = _memory_get(org_id, user.id)
    if cached is not None:
        return cached

    db_cached = _db_cache_fresh(user)
    if db_cached is not None:
        _memory_set(org_id, user.id, db_cached)
        return db_cached

    github_email: str | None = None
    if user.clerk_user_id:
        await ensure_user_github_identity(session, user)
        _, github_email = await fetch_github_identity_from_clerk(user.clerk_user_id)

    blocked_handles = await _other_member_github_handles(
        session, org_id=org_id, exclude_user_id=user.id
    )
    blocked_emails = await _other_member_emails(
        session, org_id=org_id, exclude_user_id=user.id
    )
    baseline = _baseline_profile(
        user,
        blocked_handles=blocked_handles,
        blocked_emails=blocked_emails,
        github_email=github_email,
    )
    profile = await _discover_attribution_from_tasks(
        session,
        user,
        org_id=org_id,
        baseline=baseline,
        blocked_handles=blocked_handles,
        blocked_emails=blocked_emails,
    )
    await _persist_profile(session, user, profile)
    return profile


async def _resolve_target_user_id(
    session: AsyncSession,
    auth: AuthContext,
    normalized_author: str,
) -> str | None:
    if normalized_author.lower() != "me":
        return normalized_author

    if auth.user_id:
        return auth.user_id

    api_key = auth.api_key
    if api_key is None and auth.api_key_id:
        api_key = await session.get(APIKeyModel, auth.api_key_id)
    if api_key and api_key.created_by_user_id:
        return api_key.created_by_user_id

    return None


async def resolve_experiments_author(
    session: AsyncSession,
    auth: AuthContext,
    experiments_author: str | None,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Resolve dashboard owner filter to ``(user_id, github_handles, emails)``."""
    normalized = (experiments_author or "").strip()
    if not normalized or normalized.lower() == "all":
        return None, (), ()

    target_user_id = await _resolve_target_user_id(session, auth, normalized)
    if normalized.lower() == "me" and not target_user_id:
        return UNRESOLVED_EXPERIMENTS_OWNER, (), ()

    user = await session.get(UserModel, target_user_id)
    if user is None or user.org_id != auth.org_id or not user.is_active:
        return target_user_id, (), ()

    profile = await _load_attribution_profile(session, user, org_id=auth.org_id)
    return user.id, profile.github_handles, profile.legacy_emails
