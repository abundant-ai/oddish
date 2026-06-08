from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import APIKeyScope, AuthContext, require_auth
from models import UserModel
from oddish.core.dashboard import get_dashboard_core
from oddish.db import TaskModel, get_session
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now

router = APIRouter(tags=["Dashboard"])


def _normalize_github_handle(value: str | None) -> str | None:
    normalized = (value or "").strip().lstrip("@")
    return normalized or None


def _looks_like_email(value: str) -> bool:
    return "@" in value


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


async def _discover_github_handles(
    session: AsyncSession,
    user: UserModel,
    *,
    org_id: str,
) -> tuple[str, ...]:
    """Resolve all GitHub handles that should count as Mine for this user."""
    handles: list[str] = []
    seen: set[str] = set()

    def _add(handle: str | None) -> None:
        normalized = _normalize_github_handle(handle)
        if not normalized:
            return
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        handles.append(normalized)

    _add(user.github_username)

    blocked_handles = await _other_member_github_handles(
        session, org_id=org_id, exclude_user_id=user.id
    )

    def _accept(handle: str | None) -> None:
        normalized = _normalize_github_handle(handle)
        if not normalized or normalized.lower() in blocked_handles:
            return
        _add(normalized)

    tag_expr = TaskModel.tags["github_username"].astext
    attribution_predicates = [TaskModel.created_by_user_id == user.id]
    if user.email:
        attribution_predicates.append(TaskModel.user == user.email)
    if user.github_username:
        primary = _normalize_github_handle(user.github_username)
        if primary:
            attribution_predicates.append(TaskModel.user == primary)
    # GH Actions / CLI runs often stamp tasks.user with the handle string.
    attribution_predicates.append(
        and_(
            tag_expr.isnot(None),
            tag_expr != "",
            func.lower(TaskModel.user) == func.lower(tag_expr),
        )
    )

    tag_rows = await session.execute(
        select(tag_expr)
        .where(TaskModel.org_id == org_id)
        .where(TaskModel.deleted_at.is_(None))
        .where(tag_expr.isnot(None))
        .where(tag_expr != "")
        .where(or_(*attribution_predicates))
        .distinct()
    )
    for (tag,) in tag_rows:
        _accept(tag)

    user_field_rows = await session.execute(
        select(TaskModel.user)
        .where(TaskModel.org_id == org_id)
        .where(TaskModel.deleted_at.is_(None))
        .where(TaskModel.created_by_user_id == user.id)
        .where(TaskModel.user.isnot(None))
        .where(TaskModel.user != "")
        .distinct()
    )
    for (raw_user,) in user_field_rows:
        if raw_user and not _looks_like_email(raw_user):
            _accept(raw_user)

    return tuple(handles)


def _make_timing_recorder(request: Request) -> TimingRecorder:
    def _record(name: str, duration_ms: float, description: str | None = None) -> None:
        add_server_timing_metric(request, name, duration_ms, description)

    return _record


async def _resolve_experiments_author(
    session: AsyncSession,
    auth: AuthContext,
    experiments_author: str | None,
) -> tuple[str | None, tuple[str, ...], str | None]:
    """Resolve the dashboard owner filter to ``(user_id, github_usernames, email)``.

    Accepts the ``experiments_author`` query value:
      * ``None`` / ``""`` / ``"all"`` -> no filter (whole organization)
      * ``"me"`` -> the authenticated Clerk user (``auth.user_id``)
      * a ``UserModel.id`` -> that specific organization member

    Returns ``(None, (), None)`` when no filter should apply. The resolved
    github usernames come from the user's Clerk-linked handle plus historical
    aliases discovered from their tasks (``praxs`` / ``dot-agi`` etc.), excluding
    handles registered as another org member's primary GitHub username.
    Unknown / cross-org ids keep the id (with empty handles/email) so the
    filter matches nothing rather than silently widening back to the full org.
    """
    normalized = (experiments_author or "").strip()
    if not normalized or normalized.lower() == "all":
        return None, (), None

    target_user_id = auth.user_id if normalized.lower() == "me" else normalized
    if not target_user_id:
        # "me" with an API-key principal (no user) -> no personal scope.
        return None, (), None

    user = await session.get(UserModel, target_user_id)
    if user is None or user.org_id != auth.org_id or not user.is_active:
        return target_user_id, (), None

    github_usernames = await _discover_github_handles(
        session, user, org_id=auth.org_id
    )

    return user.id, github_usernames, user.email


@router.get("/dashboard")
async def get_dashboard(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
    tasks_limit: int = Query(200, ge=1, le=500),
    tasks_offset: int = Query(0, ge=0),
    experiments_limit: int = Query(25, ge=1, le=100),
    experiments_offset: int = Query(0, ge=0),
    experiments_query: str | None = Query(None),
    experiments_status: str = Query("all"),
    experiments_author: str | None = Query(
        None,
        description=(
            "Owner filter for the experiments table: 'all' (default), "
            "'me' for the current user, or an org member's user id."
        ),
    ),
    usage_minutes: int | None = Query(None, ge=1, le=86400),
    include_tasks: bool = Query(True),
    include_usage: bool = Query(True),
    include_experiments: bool = Query(True),
) -> dict:
    """Combined dashboard endpoint returning queues, usage, tasks, and experiments.

    Response is cached for 10 seconds per organization.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        connect_started_at = now()
        await session.connection()
        add_server_timing_metric(
            request,
            "db_connect",
            elapsed_ms(connect_started_at),
            "Dashboard DB connect",
        )
        author_user_id, author_github_usernames, author_email = (
            await _resolve_experiments_author(session, auth, experiments_author)
        )
        return await get_dashboard_core(
            session,
            org_id=auth.org_id,
            tasks_limit=tasks_limit,
            tasks_offset=tasks_offset,
            experiments_limit=experiments_limit,
            experiments_offset=experiments_offset,
            experiments_query=experiments_query,
            experiments_status=experiments_status,
            experiments_author_user_id=author_user_id,
            experiments_author_github_usernames=author_github_usernames,
            experiments_author_email=author_email,
            usage_minutes=usage_minutes,
            include_tasks=include_tasks,
            include_usage=include_usage,
            include_experiments=include_experiments,
            record_timing=_make_timing_recorder(request),
        )
