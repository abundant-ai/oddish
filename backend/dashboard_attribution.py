from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.types import AuthContext
from models import APIKeyModel, UserModel
from oddish.core.dashboard import UNRESOLVED_EXPERIMENTS_OWNER
from oddish.db import TaskModel, get_session

logger = logging.getLogger(__name__)

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


def _db_cache_profile(user: UserModel) -> tuple[AttributionProfile | None, bool]:
    """Return ``(profile, fresh)`` from the user's persisted attribution cache."""
    raw = user.attribution_cache if isinstance(user.attribution_cache, dict) else None
    profile = AttributionProfile.from_dict(raw)
    if profile is None:
        return None, False
    refreshed_at = raw.get("refreshed_at") if raw else None
    if not isinstance(refreshed_at, str):
        return profile, False
    try:
        refreshed = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
    except ValueError:
        return profile, False
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - refreshed).total_seconds()
    return profile, age <= _DB_TTL_SECONDS


_refresh_in_flight: set[str] = set()
# Strong refs so fire-and-forget refresh tasks aren't garbage-collected
# mid-flight (asyncio only keeps weak refs to tasks).
_refresh_tasks: set[asyncio.Task] = set()


def _schedule_profile_refresh(*, org_id: str, user_id: str) -> None:
    """Fire-and-forget profile recompute on a fresh session.

    Serves stale profiles instantly from the request path; this keeps the
    Clerk-free discovery scan out of dashboard latency entirely.
    """
    key = _cache_key(org_id, user_id)
    if key in _refresh_in_flight:
        return
    _refresh_in_flight.add(key)

    async def _run() -> None:
        try:
            async with get_session() as session:
                user = await session.get(UserModel, user_id)
                if user is None or not user.is_active or user.org_id != org_id:
                    return
                await _compute_and_persist_profile(session, user, org_id=org_id)
        except Exception:
            logger.warning(
                "Background attribution refresh failed for %s", key, exc_info=True
            )
        finally:
            _refresh_in_flight.discard(key)

    task = asyncio.create_task(_run())
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)


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
    return AttributionProfile(
        github_handles=tuple(handles),
        legacy_emails=tuple(emails),
    )


def _row_has_strong_attribution_match(
    tag: str | None,
    raw_user: str | None,
    *,
    seen_handles: set[str],
    seen_emails: set[str],
    clerk_email: str | None = None,
) -> bool:
    """Ignore created_by-only CI rows tagged with another contributor's handle."""
    tag_normalized = _normalize_github_handle(tag)
    raw_handle = (
        None
        if not raw_user or _looks_like_email(raw_user)
        else _normalize_github_handle(raw_user)
    )
    if tag_normalized and tag_normalized.lower() in seen_handles:
        return True
    if raw_handle and raw_handle.lower() in seen_handles:
        return True
    if raw_user and _looks_like_email(raw_user) and raw_user.lower() in seen_emails:
        return True
    if (
        clerk_email
        and raw_user
        and _looks_like_email(raw_user)
        and raw_user.lower() == clerk_email.strip().lower()
    ):
        return True
    return False


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

    tag_expr = func.lower(TaskModel.tags["github_username"].astext)
    user_expr = func.lower(TaskModel.user)
    attribution_predicates = [TaskModel.created_by_user_id == user.id]
    if user.email:
        attribution_predicates.append(user_expr == user.email.lower())
    if baseline.github_handles:
        lowered_handles = [handle.lower() for handle in baseline.github_handles]
        if len(lowered_handles) == 1:
            attribution_predicates.append(tag_expr == lowered_handles[0])
            attribution_predicates.append(user_expr == lowered_handles[0])
        else:
            attribution_predicates.append(tag_expr.in_(lowered_handles))
            attribution_predicates.append(user_expr.in_(lowered_handles))

    rows = await session.execute(
        select(tag_expr, TaskModel.user)
        .where(TaskModel.org_id == org_id)
        .where(TaskModel.deleted_at.is_(None))
        .where(or_(*attribution_predicates))
        .distinct()
        .limit(200)
    )
    for tag, raw_user in rows:
        if not _row_has_strong_attribution_match(
            tag,
            raw_user,
            seen_handles=seen_handles,
            seen_emails=seen_emails,
            clerk_email=user.email,
        ):
            continue

        raw_handle = (
            None
            if not raw_user or _looks_like_email(raw_user)
            else _normalize_github_handle(raw_user)
        )
        _add_handle(tag)
        if raw_handle:
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


async def _compute_and_persist_profile(
    session: AsyncSession,
    user: UserModel,
    *,
    org_id: str,
) -> AttributionProfile:
    previous_raw = (
        user.attribution_cache if isinstance(user.attribution_cache, dict) else None
    )
    blocked_handles = await _other_member_github_handles(
        session, org_id=org_id, exclude_user_id=user.id
    )
    blocked_emails = await _other_member_emails(
        session, org_id=org_id, exclude_user_id=user.id
    )
    baseline = _baseline_profile(
        user, blocked_handles=blocked_handles, blocked_emails=blocked_emails
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
    if _profile_gained_identities(profile, previous_raw):
        from dashboard_owner_backfill import reclaim_experiments_for_user

        await reclaim_experiments_for_user(session, user=user, profile=profile)
    return profile


def _profile_gained_identities(
    profile: AttributionProfile, previous_raw: dict[str, Any] | None
) -> bool:
    previous = AttributionProfile.from_dict(previous_raw)
    if previous is None:
        return bool(profile.github_handles or profile.legacy_emails)
    return not (
        {h.lower() for h in profile.github_handles}
        <= {h.lower() for h in previous.github_handles}
        and {e.lower() for e in profile.legacy_emails}
        <= {e.lower() for e in previous.legacy_emails}
    )


async def _load_attribution_profile(
    session: AsyncSession,
    user: UserModel,
    *,
    org_id: str,
) -> AttributionProfile:
    cached = _memory_get(org_id, user.id)
    if cached is not None:
        return cached

    db_profile, fresh = _db_cache_profile(user)
    if db_profile is not None:
        _memory_set(org_id, user.id, db_profile)
        if not fresh:
            _schedule_profile_refresh(org_id=org_id, user_id=user.id)
        return db_profile

    return await _compute_and_persist_profile(session, user, org_id=org_id)


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
