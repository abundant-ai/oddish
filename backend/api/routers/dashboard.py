from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import APIKeyScope, AuthContext, require_auth
from dashboard_attribution import (
    resolve_experiments_author,
    resolve_github_users,
    resolve_search_authors,
)
from models import UserModel
from oddish.core.admin import CostLeaderboardUser, get_cost_leaderboard_core
from oddish.core.dashboard import get_dashboard_core
from oddish.db import get_session
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now

router = APIRouter(tags=["Dashboard"])


class CostLeaderboardEntry(BaseModel):
    rank: int
    name: str
    cost_usd: float


class CostLeaderboardResponse(BaseModel):
    leaders: list[CostLeaderboardEntry]


def _leaderboard_name(
    ranked: CostLeaderboardUser,
    users: dict[str, tuple[str | None, str | None, str | None]],
) -> str | None:
    """Safe display label: name, else @handle, else the email local part.

    A GitHub-identity fallback bucket has no registered user and carries its
    own precomputed ``@handle`` label. For registered users the email local
    part keeps unlinked accounts on the board; the full address must never
    be returned.
    """
    if ranked.user_id is None:
        return (ranked.label or "").strip() or None
    user_row = users.get(ranked.user_id)
    if user_row is None:
        return None
    user_name, github_username, email = user_row
    name = (user_name or "").strip()
    if name:
        return name
    handle = (github_username or "").strip().lstrip("@")
    if handle:
        return f"@{handle}"
    return (email or "").split("@", 1)[0].strip() or None


def _leaderboard_entries(
    ranked_users: list[CostLeaderboardUser],
    users: dict[str, tuple[str | None, str | None, str | None]],
    *,
    limit: int,
    rank_offset: int = 0,
) -> list[CostLeaderboardEntry]:
    """Project internal ids down to the leaderboard's intentionally tiny API."""
    leaders: list[CostLeaderboardEntry] = []
    for rank, ranked in enumerate(ranked_users, start=rank_offset + 1):
        name = _leaderboard_name(ranked, users)
        if not name:
            continue
        leaders.append(
            CostLeaderboardEntry(rank=rank, name=name, cost_usd=ranked.cost_usd)
        )
        if len(leaders) >= limit:
            break
    return leaders


@router.get("/leaderboard", response_model=CostLeaderboardResponse)
async def get_cost_leaderboard(
    auth: Annotated[AuthContext, Depends(require_auth)],
    window_days: int = Query(
        7, ge=0, le=3650, description="Trailing window in days; 0 = all-time"
    ),
    limit: int = Query(100, ge=1, le=500),
) -> CostLeaderboardResponse:
    """Return only ranked display names and spend, with no admin cost metadata."""
    effective_window = None if window_days == 0 else window_days
    async with get_session() as session:
        ranked_users = await get_cost_leaderboard_core(
            session,
            org_id=auth.org_id,
            window_days=effective_window,
            resolve_github_users=resolve_github_users,
        )
        leaders: list[CostLeaderboardEntry] = []
        for offset in range(0, len(ranked_users), limit):
            batch = ranked_users[offset : offset + limit]
            batch_user_ids = [entry.user_id for entry in batch if entry.user_id]
            users: dict[str, tuple[str | None, str | None, str | None]] = {}
            if batch_user_ids:
                rows = await session.execute(
                    select(
                        UserModel.id,
                        UserModel.name,
                        UserModel.github_username,
                        UserModel.email,
                    )
                    .where(
                        UserModel.id.in_(batch_user_ids),
                        UserModel.org_id == auth.org_id,
                    )
                    .execution_options(include_deleted=True)
                )
                users = {
                    user_id: (name, github_username, email)
                    for user_id, name, github_username, email in rows.all()
                }
            leaders.extend(
                _leaderboard_entries(
                    batch,
                    users,
                    limit=limit - len(leaders),
                    rank_offset=offset,
                )
            )
            if len(leaders) >= limit:
                break
    return CostLeaderboardResponse(leaders=leaders)


def _member_label(user: UserModel) -> dict[str, str] | None:
    """Canonical label for a resolved member: name beats handle, else nothing.

    Email is deliberately not a fallback -- it must never be *promoted* into a
    dashboard label (PII on a widely-visible page).
    """
    if user.name:
        return {"name": user.name, "source": "member"}
    if user.github_username:
        return {"name": user.github_username, "source": "github"}
    return None


def _normalize_label_key(value: Any) -> str | None:
    """Normalize a raw author/runner string for member lookup."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.startswith("@"):
        stripped = stripped[1:]
    return stripped.lower() or None


async def _enrich_experiment_authors(
    session: AsyncSession, dashboard: dict[str, Any], *, org_id: str | None
) -> None:
    """Promote canonical org-member labels into ``author`` and ``last_runner``.

    The ``oddish`` core layer can't import the cloud auth tables, so it emits
    the experiments-list ``author`` / ``last_runner`` from handle/legacy-user
    strings plus two raw internal user ids per row: ``owner_user_id`` (the
    experiment's set-once owner) and ``last_runner_user_id`` (the latest
    trial's ``billed_user_id`` -- per-run identity, correct across APPENDs to
    shared tasks). Each field is resolved through three tiers, preserving the
    agreed precedence member name -> @github handle -> raw string -> "—":

    1. **Id -> name**: the row's user id resolves to a member with a non-empty
       ``name`` -> ``{"name": <name>, "source": "member"}``.
    2. **Id -> handle**: the id resolves but the member has no display name
       (the JWT provisioning path creates users without one); fall back to
       their ``github_username`` -> ``{"name": <handle>, "source": "github"}``
       so at least the label is canonical.
    3. **String match**: rows with no resolvable id (legacy experiments with
       NULL ``owner_user_id`` / legacy trials with NULL ``billed_user_id``)
       carry a raw task string -- an email, ``@handle``, or the
       ``{clerk_id}@clerk.user`` sentinel. If that string is exactly one
       active org member's email or github handle (ambiguous handles/emails
       are skipped), re-label it via the same name-then-handle precedence.
       This never changes *which person* is displayed -- it only canonicalizes
       a string that already identifies that member.

    Email is never promoted into a label; this only ever replaces an email
    with a name/handle, never the reverse. At most two queries per request:
    the org's active users, plus one ``include_deleted=True`` id lookup for
    referenced ids not found among them (historical/deactivated owners --
    mirrors the cost path).

    The experiment row dicts are the *same objects* the core layer stores in
    its module-level experiments cache, so this function must never mutate
    them: doing so would destroy the cached github/api fallback for the rest
    of the cache TTL (and race concurrent requests sharing the cached list).
    Enriched rows are shallow copies; the top-level ``dashboard`` dict is a
    fresh per-request merge, so reassigning its ``experiments`` key is safe.
    ``last_author`` (deprecated mirror of ``last_runner``) is kept in sync.
    """
    experiments = dashboard.get("experiments")
    if not experiments:
        return

    referenced_ids = {
        user_id
        for row in experiments
        if isinstance(row, dict)
        for user_id in (row.get("owner_user_id"), row.get("last_runner_user_id"))
        if user_id
    }

    users_by_id: dict[str, UserModel] = {}
    by_email: dict[str, UserModel] = {}
    by_handle: dict[str, UserModel] = {}

    if org_id:
        rows = await session.execute(
            select(UserModel).where(
                UserModel.org_id == org_id,
                UserModel.is_active.is_(True),
            )
        )
        email_buckets: dict[str, list[UserModel]] = {}
        handle_buckets: dict[str, list[UserModel]] = {}
        for user in rows.scalars():
            users_by_id[user.id] = user
            if user.email:
                email_buckets.setdefault(user.email.strip().lower(), []).append(user)
            if user.github_username:
                handle_buckets.setdefault(
                    user.github_username.strip().lower(), []
                ).append(user)
        # Exactly-one semantics (mirrors ``lookup_users_by_github_username`` on
        # the submission path): a handle -- or, defensively, an email -- shared
        # by two active members is ambiguous and must not resolve.
        by_email = {k: v[0] for k, v in email_buckets.items() if len(v) == 1}
        by_handle = {k: v[0] for k, v in handle_buckets.items() if len(v) == 1}

    missing_ids = {i for i in referenced_ids if i not in users_by_id}
    if missing_ids:
        rows = await session.execute(
            select(UserModel)
            .where(UserModel.id.in_(missing_ids))
            .execution_options(include_deleted=True)
        )
        for user in rows.scalars():
            users_by_id[user.id] = user

    def _resolve(row: dict[str, Any], id_key: str, value_key: str) -> dict | None:
        # Tiers 1+2: the internal user id, labeled name-then-handle. An id is
        # TERMINAL: it already identifies the person, so when it yields no
        # promotable label (user unresolvable, or no name and no handle) we
        # keep the core value rather than fall through to the string tier --
        # the fallback string can name a *different* person (e.g. the task
        # creator on an appended run) and relabeling it would silently swap
        # who is displayed.
        user_id = row.get(id_key)
        if user_id:
            user = users_by_id.get(user_id)
            if user is None:
                return None
            return _member_label(user)
        # Tier 3 (no id at all): match the raw string label to exactly one
        # active member.
        current = row.get(value_key)
        if isinstance(current, dict):
            key = _normalize_label_key(current.get("name"))
            if key:
                match = by_email.get(key) or by_handle.get(key)
                if match is not None:
                    return _member_label(match)
        return None

    enriched: list[Any] = []
    for row in experiments:
        if isinstance(row, dict):
            overrides: dict[str, Any] = {}
            author_label = _resolve(row, "owner_user_id", "author")
            if author_label and author_label != row.get("author"):
                overrides["author"] = author_label
            runner_label = _resolve(row, "last_runner_user_id", "last_runner")
            if runner_label and runner_label != row.get("last_runner"):
                overrides["last_runner"] = runner_label
                # The core emits ``last_author`` as a mirror of ``last_runner``
                # (deprecated fallback the FE still renders); keep them in sync.
                overrides["last_author"] = runner_label
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
            await _enrich_experiment_authors(session, dashboard, org_id=auth.org_id)
            add_server_timing_metric(
                request,
                "dashboard_author_enrich",
                elapsed_ms(enrich_started_at),
                "Dashboard author member-name enrich",
            )
        return dashboard
