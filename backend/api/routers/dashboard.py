from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import APIKeyScope, AuthContext, require_auth
from models import UserModel
from oddish.core.dashboard import get_dashboard_core
from oddish.db import TaskModel, get_session
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now

router = APIRouter(tags=["Dashboard"])


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
    github usernames come from the user's primary Clerk-linked handle plus
    exclusive historical aliases (tags only seen on their tasks, never on
    tasks attributed to another org member).
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

    github_usernames: list[str] = []
    if user.github_username:
        normalized_primary = user.github_username.strip().lstrip("@")
        if normalized_primary:
            github_usernames.append(normalized_primary)

    alias_predicates = [TaskModel.created_by_user_id == user.id]
    if user.email:
        alias_predicates.append(TaskModel.user == user.email)

    alias_rows = await session.execute(
        select(TaskModel.tags["github_username"].astext)
        .where(TaskModel.org_id == auth.org_id)
        .where(TaskModel.deleted_at.is_(None))
        .where(TaskModel.tags["github_username"].astext.isnot(None))
        .where(or_(*alias_predicates))
        .distinct()
    )
    for (alias,) in alias_rows:
        normalized_alias = (alias or "").strip().lstrip("@")
        if not normalized_alias or normalized_alias in github_usernames:
            continue
        other_owner_count = await session.scalar(
            select(func.count())
            .select_from(TaskModel)
            .where(TaskModel.org_id == auth.org_id)
            .where(TaskModel.deleted_at.is_(None))
            .where(TaskModel.tags["github_username"].astext == normalized_alias)
            .where(TaskModel.created_by_user_id.isnot(None))
            .where(TaskModel.created_by_user_id != user.id)
        )
        if other_owner_count:
            continue
        github_usernames.append(normalized_alias)

    return user.id, tuple(github_usernames), user.email


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
