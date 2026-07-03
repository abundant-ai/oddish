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
    """Promote canonical org-member names into ``author`` and ``last_runner``.

    The ``oddish`` core layer can't import the cloud auth tables, so it emits
    the experiments-list ``author`` / ``last_runner`` from handle/legacy-user
    strings plus two raw internal user ids per row:

    - ``owner_user_id``: the experiment's set-once owner. When it resolves to
      a member with a non-empty display name, ``author`` becomes
      ``{"name": <member name>, "source": "member"}`` so the Author column
      matches the cost page's canonical name.
    - ``last_runner_user_id``: the latest trial's ``billed_user_id`` (per-run
      identity, correct across APPENDs to shared tasks). When it resolves to a
      named member, ``last_runner`` is overridden the same way; otherwise the
      core's task-based fallback (the task creator's handle/string) stands.

    Both resolve through one batched ``UserModel`` load. Precedence for each
    rendered label is member name -> @github handle -> raw owner/user string
    -> "—"; the last three tiers are already baked into the values the core
    computed, so we only override the top tier. ``include_deleted=True``
    mirrors the cost path so a historical owner still resolves. Email is
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

    user_ids = {
        user_id
        for row in experiments
        if isinstance(row, dict)
        for user_id in (row.get("owner_user_id"), row.get("last_runner_user_id"))
        if user_id
    }
    if not user_ids:
        return

    rows = await session.execute(
        select(UserModel)
        .where(UserModel.id.in_(user_ids))
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
        if isinstance(row, dict):
            overrides: dict[str, Any] = {}
            owner_name = names.get(row.get("owner_user_id"))
            if owner_name:
                overrides["author"] = {"name": owner_name, "source": "member"}
            runner_name = names.get(row.get("last_runner_user_id"))
            if runner_name:
                runner = {"name": runner_name, "source": "member"}
                overrides["last_runner"] = runner
                # The core emits ``last_author`` as a mirror of ``last_runner``
                # (deprecated fallback the FE still renders); keep them in sync.
                overrides["last_author"] = runner
            if overrides:
                row = {**row, **overrides}
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
