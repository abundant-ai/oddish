from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import APIKeyScope, AuthContext, require_auth
from dashboard_attribution import resolve_experiments_author, resolve_search_authors
from models import UserModel
from oddish.core.dashboard import get_dashboard_core
from oddish.db import get_session
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now

router = APIRouter(tags=["Dashboard"])


async def _enrich_experiment_authors(
    session: AsyncSession, dashboard: dict[str, Any]
) -> None:
    """Promote each experiment's canonical org-member name into ``author``.

    The ``oddish`` core layer can't import the cloud auth tables, so it emits
    the experiments-list ``author`` from the handle/legacy-user strings plus a
    raw internal ``owner_user_id``. Here we batch-resolve those owner ids to
    ``UserModel`` rows and, when an owner has a non-empty display name, rewrite
    ``author`` to ``{"name": <member name>, "source": "member"}`` so the Author
    column matches the cost page's canonical name.

    Precedence for the rendered label is therefore member name -> @github handle
    -> raw owner/user string -> "—"; the last three tiers are already baked into
    the ``author`` value the core computed, so we only override the top tier.
    Owners with no resolvable name (or the sentinel, already None-d out in core)
    keep the core value. ``include_deleted=True`` mirrors the cost path so a
    historical owner who has since been deactivated still resolves. Email is
    intentionally never promoted here (PII on a widely-visible page).

    The experiment row dicts are the *same objects* the core layer stores in
    its module-level experiments cache, so this function must never mutate
    them: doing so would destroy the cached github/api fallback for the rest
    of the cache TTL (and race concurrent requests sharing the cached list).
    Enriched rows are shallow copies; the top-level ``dashboard`` dict is a
    fresh per-request merge, so reassigning its ``experiments`` key is safe.
    """
    experiments = dashboard.get("experiments")
    if not experiments:
        return

    owner_ids = {
        row["owner_user_id"]
        for row in experiments
        if isinstance(row, dict) and row.get("owner_user_id")
    }
    if not owner_ids:
        return

    rows = await session.execute(
        select(UserModel)
        .where(UserModel.id.in_(owner_ids))
        .execution_options(include_deleted=True)
    )
    names: dict[str, str] = {}
    for user in rows.scalars():
        if user.name:
            names[user.id] = user.name

    if not names:
        return

    enriched: list[Any] = []
    for row in experiments:
        member_name = (
            names.get(row.get("owner_user_id")) if isinstance(row, dict) else None
        )
        if member_name:
            row = {**row, "author": {"name": member_name, "source": "member"}}
        enriched.append(row)
    dashboard["experiments"] = enriched


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
        dashboard = await get_dashboard_core(
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
        # Resolve canonical member names for the Author column. Runs on every
        # request (cache hit or miss) so cached and fresh responses agree. The
        # enrichment copies rows rather than mutating them, so the core's
        # cached github/api fallback values survive intact.
        if include_experiments:
            enrich_started_at = now()
            await _enrich_experiment_authors(session, dashboard)
            add_server_timing_metric(
                request,
                "dashboard_author_enrich",
                elapsed_ms(enrich_started_at),
                "Dashboard author member-name enrich",
            )
        return dashboard
