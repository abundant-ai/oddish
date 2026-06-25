from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from auth import APIKeyScope, AuthContext, require_auth
from dashboard_attribution import resolve_experiments_author, resolve_search_authors
from oddish.core.dashboard import get_dashboard_core
from oddish.db import get_session
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now

router = APIRouter(tags=["Dashboard"])


def _make_timing_recorder(request: Request) -> TimingRecorder:
    def _record(name: str, duration_ms: float, description: str | None = None) -> None:
        add_server_timing_metric(request, name, duration_ms, description)

    return _record


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
    experiments_tags: str | None = Query(None, description="CSV tag tokens, AND"),
    experiments_tags_any: str | None = Query(None, description="CSV tag tokens, OR"),
    experiments_tags_none: str | None = Query(None, description="CSV tag tokens, NOT"),
    experiments_author: str | None = Query(
        None,
        description=(
            "Owner filter for the experiments table: 'all' (default), "
            "'me' for the current user, or an org member's user id."
        ),
    ),
    experiments_author_query: str | None = Query(
        None,
        description=(
            "Free author search for the experiments table (the github:/author:/"
            "user: qualifier). Comma-separated tokens, each resolved to matching "
            "org members + their aliases and ANDed with the owner/tag filters."
        ),
    ),
    usage_minutes: int | None = Query(None, ge=1, le=86400),
    include_queues: bool = Query(True),
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
        resolve_started_at = now()
        author_user_id, author_github_usernames, author_emails = (
            await resolve_experiments_author(session, auth, experiments_author)
        )
        search_tokens = [
            token.strip()
            for token in (experiments_author_query or "").split(",")
            if token.strip()
        ]
        if search_tokens:
            (
                search_author_user_ids,
                search_author_github_usernames,
                search_author_emails,
            ) = await resolve_search_authors(
                session, org_id=auth.org_id, tokens=search_tokens
            )
        else:
            search_author_user_ids = ()
            search_author_github_usernames = ()
            search_author_emails = ()
        add_server_timing_metric(
            request,
            "dashboard_author_resolve",
            elapsed_ms(resolve_started_at),
            "Dashboard author filter resolve",
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
            experiments_tags=experiments_tags,
            experiments_tags_any=experiments_tags_any,
            experiments_tags_none=experiments_tags_none,
            experiments_author_user_id=author_user_id,
            experiments_author_github_usernames=author_github_usernames,
            experiments_author_emails=author_emails,
            experiments_search_author_user_ids=search_author_user_ids,
            experiments_search_author_github_usernames=search_author_github_usernames,
            experiments_search_author_emails=search_author_emails,
            usage_minutes=usage_minutes,
            include_queues=include_queues,
            include_tasks=include_tasks,
            include_usage=include_usage,
            include_experiments=include_experiments,
            record_timing=_make_timing_recorder(request),
        )
