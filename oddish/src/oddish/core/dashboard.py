from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    and_,
    case,
    false,
    func,
    not_,
    nulls_last,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.helpers import build_task_status_responses_from_counts
from oddish.core.tag_filter_ast import (
    ResolvedTagFilter,
    TagFilterAST,
    resolve_names_to_ids,
)
from oddish.core.tags_projection import (
    UserTagView,
    list_direct_tags_for_targets,
    list_effective_user_tags_for_task_versions,
)
from oddish.config import normalize_model_id
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
    task_experiments,
)
from oddish.queue import get_queue_and_pipeline_stats_with_concurrency
from oddish.timing import TimingRecorder, elapsed_ms, now

logger = logging.getLogger(__name__)

# Sentinel user id: Mine filter requested but no resolvable Clerk/API-key owner.
UNRESOLVED_EXPERIMENTS_OWNER = "__unresolved_owner__"

# Sentinel owner: stamped by the cloud sweep on live experiments that no org
# member's attribution profile claims. Lets the Mine filter switch to the
# indexed owner_user_id fast path once an org has zero NULL owners, while
# never matching a real user id.
EXPERIMENTS_UNATTRIBUTED_OWNER = "__unattributed__"


def _parse_github_meta(raw_github_meta: str | None) -> dict[str, Any] | None:
    if not raw_github_meta:
        return None
    try:
        parsed = json.loads(raw_github_meta)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# Matches a PR number in a GitHub URL (.../pull/123 or .../pulls/123), mirroring
# the frontend's ``parsePrNumberFromUrl`` so the dashboard PR badge can show
# ``#<number>`` even when the URL came from the ``link`` column rather than
# structured ``github_meta``.
_PR_NUMBER_FROM_URL = re.compile(r"/pulls?/(\d+)(?:[/?#]|$)")


def _pr_number_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = _PR_NUMBER_FROM_URL.search(url)
    return match.group(1) if match else None


def _normalize_dashboard_model(model: str | None, provider: str | None) -> str:
    """Preserve the nop/oracle default model label in usage tables."""
    normalized_model = normalize_model_id(model)
    if normalized_model:
        return normalized_model

    normalized_provider = (provider or "").strip().lower()
    raw_model = (model or "").strip().lower()
    if raw_model == "default" or normalized_provider == "default":
        return "default"

    return "unknown"


# ---------------------------------------------------------------------------
# Response Caching
# ---------------------------------------------------------------------------
#
# Two independent slices so that filter/page changes on one half don't
# invalidate the other. The previous single-key cache forced an
# all-or-nothing recompute every time a query / status filter / time
# range changed, which made the cache TTL effectively zero in practice.
#
# The "experiments slice" carries the recent-experiments table; the
# "primary slice" carries queue stats, pipeline stats, model usage,
# worker_job usage, and the recent-tasks list. Both share the same
# bucketed-LRU bookkeeping but live in their own dicts so eviction
# pressure on one doesn't churn the other.

_CACHE_MAX_SIZE = 100
_EXPERIMENTS_CACHE_TTL_SECONDS = 30
_PRIMARY_CACHE_TTL_SECONDS = 60
# Queue + pipeline stats are a full aggregate over the entire ``trials`` table
# (a parallel seq scan of the whole heap). They depend only on ``org_id`` --
# not on task pagination, usage window, or the include_* flags that key the
# primary slice -- so bundling them into the primary cache made that expensive
# scan re-run on every pagination / usage-window variation. Caching them in
# their own org-keyed slice means the trials scan runs at most once per org per
# TTL no matter how the rest of the dashboard request varies.
_QUEUE_PIPELINE_CACHE_TTL_SECONDS = 30

_dashboard_experiments_cache: dict[str, tuple[Any, float]] = {}
_dashboard_primary_cache: dict[str, tuple[Any, float]] = {}
_dashboard_queue_pipeline_cache: dict[str, tuple[Any, float]] = {}


def _slice_get_cached(
    bucket: dict[str, tuple[Any, float]], cache_key: str, ttl_seconds: int
) -> Any | None:
    if cache_key not in bucket:
        return None
    cached, cached_at = bucket[cache_key]
    if time.time() - cached_at > ttl_seconds:
        del bucket[cache_key]
        return None
    return cached


def _slice_set_cached(
    bucket: dict[str, tuple[Any, float]], cache_key: str, data: Any
) -> None:
    if len(bucket) >= _CACHE_MAX_SIZE:
        sorted_keys = sorted(bucket.keys(), key=lambda k: bucket[k][1])
        for k in sorted_keys[: _CACHE_MAX_SIZE // 4]:
            del bucket[k]
    bucket[cache_key] = (data, time.time())


def invalidate_dashboard_cache(*, org_id: str | None = None) -> None:
    """Clear cached dashboard slices after writes that change visible rows."""
    if org_id is None:
        _dashboard_primary_cache.clear()
        _dashboard_experiments_cache.clear()
        _dashboard_queue_pipeline_cache.clear()
        return

    prefixes = (
        f"dashboard.primary:{org_id}:",
        f"dashboard.experiments:{org_id}:",
        f"dashboard.queue_pipeline:{org_id}:",
    )
    for bucket in (
        _dashboard_primary_cache,
        _dashboard_experiments_cache,
        _dashboard_queue_pipeline_cache,
    ):
        for key in list(bucket):
            if key.startswith(prefixes):
                del bucket[key]


# ---------------------------------------------------------------------------
# Experiment aggregation
# ---------------------------------------------------------------------------


# Status filters depend on aggregated trial/verdict counts that we
# can't apply until after per-experiment aggregation. To make those
# filters cheap we over-fetch a wider window of experiments by
# ``last_activity_at`` and let the post-aggregation filter trim it.
# The multiplier and ceiling are small enough to keep the page query
# tight while still returning a full page in the common case.
_STATUS_FILTER_OVERFETCH_MULTIPLIER = 4
_STATUS_FILTER_OVERFETCH_CEILING = 200


def _baseline_agent_clause():
    """Match nop/oracle baseline agents, mirroring the frontend's
    ``isBaselineAgentName`` so pass@1 excludes deterministic baselines."""
    agent_lower = func.lower(func.coalesce(TrialModel.agent, ""))
    return or_(
        agent_lower == "nop",
        agent_lower == "oracle",
        agent_lower.like("nop-%"),
        agent_lower.like("oracle-%"),
        agent_lower.like("agent-nop%"),
        agent_lower.like("agent-oracle%"),
    )


def _build_aggregates_for_experiment_ids(
    experiment_ids: list[str], *, org_id: str | None
):
    """Return (task_agg_subquery, trial_agg_subquery, score_agg_subquery)
    scoped to a page.

    The subqueries restrict their FROM-side to the given experiment ids
    so the planner walks only ``len(experiment_ids)`` rows worth of
    tasks/trials instead of the org's full set. Org scoping is also
    applied for defense in depth -- ``last_activity_at`` is denormalized
    onto the experiment row so the page lookup already filters by org,
    but any caller passing a stale page from a different org still sees
    only their own data.
    """
    task_agg_query = (
        select(
            task_experiments.c.experiment_id.label("experiment_id"),
            func.count(TaskModel.id).label("task_count"),
            func.count(case((TaskModel.run_analysis.is_(True), 1))).label(
                "analysis_tasks"
            ),
            func.count(
                case(
                    (
                        and_(
                            TaskModel.verdict_status == VerdictStatus.SUCCESS,
                            TaskModel.verdict["is_good"].astext == "true",
                        ),
                        1,
                    )
                )
            ).label("verdict_good"),
            func.count(
                case(
                    (
                        and_(
                            TaskModel.verdict_status == VerdictStatus.SUCCESS,
                            TaskModel.verdict["is_good"].astext == "false",
                        ),
                        1,
                    )
                )
            ).label("verdict_needs_review"),
            func.count(
                case((TaskModel.verdict_status == VerdictStatus.FAILED, 1))
            ).label("verdict_failed"),
            func.count(
                case(
                    (
                        and_(
                            TaskModel.run_analysis.is_(True),
                            or_(
                                TaskModel.verdict_status.is_(None),
                                TaskModel.verdict_status.in_(
                                    [
                                        VerdictStatus.PENDING,
                                        VerdictStatus.QUEUED,
                                        VerdictStatus.RUNNING,
                                    ]
                                ),
                                TaskModel.status.in_(
                                    [
                                        TaskStatus.ANALYZING,
                                        TaskStatus.VERDICT_PENDING,
                                    ]
                                ),
                            ),
                        ),
                        1,
                    )
                )
            ).label("verdict_pending"),
            func.max(TaskModel.created_at).label("last_task_created_at"),
        )
        .select_from(
            task_experiments.join(
                TaskModel,  # type: ignore[arg-type]
                TaskModel.id == task_experiments.c.task_id,
            )
        )
        .where(task_experiments.c.experiment_id.in_(experiment_ids))
        .where(task_experiments.c.deleted_at.is_(None))
    )
    if org_id is not None:
        task_agg_query = task_agg_query.where(TaskModel.org_id == org_id)
    task_agg = task_agg_query.group_by(task_experiments.c.experiment_id).subquery()

    trial_agg_query = select(
        TrialModel.experiment_id.label("experiment_id"),
        func.max(TrialModel.created_at).label("last_trial_created_at"),
        func.count(func.distinct(TrialModel.task_id)).label("trial_task_count"),
        func.count(TrialModel.id).label("total_trials"),
        func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
            "completed_trials"
        ),
        func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
            "failed_trials"
        ),
        func.count(case((TrialModel.status == TrialStatus.RETRYING, 1))).label(
            "retrying_trials"
        ),
        func.count(
            case(
                (
                    TrialModel.status.in_(
                        [
                            TrialStatus.PENDING,
                            TrialStatus.QUEUED,
                            TrialStatus.RUNNING,
                            TrialStatus.RETRYING,
                        ]
                    ),
                    1,
                )
            )
        ).label("active_trials"),
        func.count(case((TrialModel.reward == 1, 1))).label("reward_success"),
        func.sum(TrialModel.reward).label("reward_sum"),
        func.count(case((TrialModel.reward.isnot(None), 1))).label("reward_total"),
    ).where(
        TrialModel.experiment_id.in_(experiment_ids),
        TrialModel.superseded_by_trial_id.is_(None),
    )
    if org_id is not None:
        trial_agg_query = trial_agg_query.where(TrialModel.org_id == org_id)
    trial_agg = trial_agg_query.group_by(TrialModel.experiment_id).subquery()

    # avg score: per-task mean reward (over scored trials) averaged across
    # tasks, so tasks with many trials don't dominate the experiment score
    # and partial credit averages in. nop/oracle baseline trials are
    # excluded.
    per_task_score_query = (
        select(
            TrialModel.experiment_id.label("experiment_id"),
            func.avg(TrialModel.reward).label("task_avg_score"),
        )
        .where(
            TrialModel.experiment_id.in_(experiment_ids),
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.reward.isnot(None),
            not_(_baseline_agent_clause()),
        )
        .group_by(TrialModel.experiment_id, TrialModel.task_id)
    )
    if org_id is not None:
        per_task_score_query = per_task_score_query.where(TrialModel.org_id == org_id)
    per_task_score = per_task_score_query.subquery()
    score_agg = (
        select(
            per_task_score.c.experiment_id.label("experiment_id"),
            func.avg(per_task_score.c.task_avg_score).label("avg_score"),
        )
        .group_by(per_task_score.c.experiment_id)
        .subquery()
    )

    return task_agg, trial_agg, score_agg


def _first_live_task_id_for_experiment():
    """Correlated subquery: oldest live task id linked to the experiment row."""
    return (
        select(TaskModel.id)
        .select_from(
            task_experiments.join(
                TaskModel,  # type: ignore[arg-type]
                TaskModel.id == task_experiments.c.task_id,
            )
        )
        .where(task_experiments.c.experiment_id == ExperimentModel.id)
        .where(task_experiments.c.deleted_at.is_(None))
        .where(TaskModel.deleted_at.is_(None))
        .order_by(TaskModel.created_at.asc(), TaskModel.id.asc())
        .limit(1)
        .correlate(ExperimentModel)
        .scalar_subquery()
    )


def _latest_live_task_id_for_experiment():
    """Correlated subquery: newest live task id linked to the experiment row."""
    return (
        select(TaskModel.id)
        .select_from(
            task_experiments.join(
                TaskModel,  # type: ignore[arg-type]
                TaskModel.id == task_experiments.c.task_id,
            )
        )
        .where(task_experiments.c.experiment_id == ExperimentModel.id)
        .where(task_experiments.c.deleted_at.is_(None))
        .where(TaskModel.deleted_at.is_(None))
        .order_by(TaskModel.created_at.desc(), TaskModel.id.desc())
        .limit(1)
        .correlate(ExperimentModel)
        .scalar_subquery()
    )


def _normalize_github_handle(value: str | None) -> str | None:
    normalized = (value or "").strip().lstrip("@")
    return normalized or None


def _task_github_tag_expr():
    return TaskModel.tags["github_username"].astext


def _empty_github_tag_clause():
    github_tag = _task_github_tag_expr()
    return or_(github_tag.is_(None), github_tag == "")


def _absent_legacy_user_clause():
    """Treat null, empty, and placeholder ``unknown`` as no legacy user string."""
    return or_(
        TaskModel.user.is_(None),
        TaskModel.user == "",
        func.lower(TaskModel.user) == "unknown",
    )


def _dashboard_author_from_task(
    *,
    github_username: str | None,
    user: str | None,
) -> dict[str, str] | None:
    name = github_username or user
    if not name:
        return None
    return {
        "name": name,
        "source": "github" if github_username else "api",
    }


def _build_primary_task_author_match(
    experiments_author_user_id: str,
    github_handles: list[str],
    *,
    experiments_author_emails: Sequence[str] | None,
):
    """Match the dashboard Author column on the experiment's oldest live task.

    Precedence mirrors ``primary_github_username or primary_user`` in the response:
    ``github_username`` tag first, then legacy ``tasks.user`` (Clerk email,
    GitHub-linked email such as ``ps4534@nyu.edu``, or known handle strings
    from GH Actions / CLI ``--github-user``), then ``created_by_user_id`` only
    when neither is present.
    """
    github_tag = _task_github_tag_expr()
    normalized_emails = [
        email.strip()
        for email in (experiments_author_emails or ())
        if (email or "").strip()
    ]

    lowered_handles = [handle.lower() for handle in github_handles if handle]
    lowered_tag = func.lower(github_tag)
    lowered_user = func.lower(TaskModel.user)

    tiers = []
    if len(lowered_handles) == 1:
        tiers.append(lowered_tag == lowered_handles[0])
    elif len(lowered_handles) > 1:
        tiers.append(lowered_tag.in_(lowered_handles))

    legacy_user_matches = []
    lowered_emails = [email.lower() for email in normalized_emails]
    if len(lowered_emails) == 1:
        legacy_user_matches.append(lowered_user == lowered_emails[0])
    elif len(lowered_emails) > 1:
        legacy_user_matches.append(lowered_user.in_(lowered_emails))
    if len(lowered_handles) == 1:
        legacy_user_matches.append(lowered_user == lowered_handles[0])
    elif len(lowered_handles) > 1:
        legacy_user_matches.append(lowered_user.in_(lowered_handles))
    if legacy_user_matches:
        tiers.append(and_(_empty_github_tag_clause(), or_(*legacy_user_matches)))

    tiers.append(
        and_(
            _empty_github_tag_clause(),
            _absent_legacy_user_clause(),
            TaskModel.created_by_user_id == experiments_author_user_id,
        )
    )
    return or_(*tiers)


def _build_experiments_author_filter(
    experiments_author_user_id: str | None,
    experiments_author_github_usernames: Sequence[str] | None,
    *,
    org_id: str | None,
    experiments_author_emails: Sequence[str] | None = None,
    include_legacy_fallback: bool = True,
):
    """EXISTS clause restricting experiments to a single owner, or ``None``.

    Returns ``None`` when no owner filter is requested. Otherwise requires
    the experiment's **oldest live task** (primary owner) to match using the
    same attribution precedence as the dashboard Author column:
    ``github_username`` tag first, then legacy ``tasks.user`` values (emails
    and handles), then ``created_by_user_id`` only when neither is present.

    When ``include_legacy_fallback`` is False (the org has zero NULL-owner
    live experiments) only the indexed ``owner_user_id`` seek is emitted.
    """
    if experiments_author_user_id is None:
        return None
    if experiments_author_user_id in (
        UNRESOLVED_EXPERIMENTS_OWNER,
        EXPERIMENTS_UNATTRIBUTED_OWNER,
    ):
        return false()

    # Fast path: indexed owner column when stamped at submit time.
    owner_match = ExperimentModel.owner_user_id == experiments_author_user_id
    if not include_legacy_fallback:
        return owner_match

    github_handles = [
        handle
        for handle in (
            _normalize_github_handle(name)
            for name in (experiments_author_github_usernames or ())
        )
        if handle
    ]

    primary_task_id = _first_live_task_id_for_experiment()
    legacy_exists = (
        select(1)
        .select_from(TaskModel)
        .where(TaskModel.id == primary_task_id)
        .where(
            _build_primary_task_author_match(
                experiments_author_user_id,
                github_handles,
                experiments_author_emails=experiments_author_emails,
            )
        )
    )
    if org_id is not None:
        legacy_exists = legacy_exists.where(TaskModel.org_id == org_id)
    legacy_match = and_(
        ExperimentModel.owner_user_id.is_(None),
        legacy_exists.exists(),
    )
    return or_(owner_match, legacy_match)


def _build_primary_task_search_match(
    user_ids: Sequence[str],
    github_handles: Sequence[str],
    *,
    emails: Sequence[str] | None,
):
    """Primary-task author match for a *set* of identities, or ``None``.

    Iterable sibling of ``_build_primary_task_author_match`` used by the
    ``github:`` search qualifier (a handle can resolve to several users +
    aliases). Same attribution precedence — ``github_username`` tag, then
    legacy ``tasks.user`` (emails/handles), then ``created_by_user_id`` — but
    every tier uses ``IN`` so collisions union. Returns ``None`` when no
    identity was supplied.
    """
    lowered_handles = [handle.lower() for handle in github_handles if handle]
    lowered_emails = [
        email.strip().lower() for email in (emails or ()) if (email or "").strip()
    ]
    normalized_user_ids = [uid for uid in (user_ids or ()) if uid]
    lowered_tag = func.lower(_task_github_tag_expr())
    lowered_user = func.lower(TaskModel.user)

    tiers = []
    if lowered_handles:
        tiers.append(lowered_tag.in_(lowered_handles))

    legacy_user_matches = []
    if lowered_emails:
        legacy_user_matches.append(lowered_user.in_(lowered_emails))
    if lowered_handles:
        legacy_user_matches.append(lowered_user.in_(lowered_handles))
    if legacy_user_matches:
        tiers.append(and_(_empty_github_tag_clause(), or_(*legacy_user_matches)))

    if normalized_user_ids:
        tiers.append(
            and_(
                _empty_github_tag_clause(),
                _absent_legacy_user_clause(),
                TaskModel.created_by_user_id.in_(normalized_user_ids),
            )
        )

    if not tiers:
        return None
    return or_(*tiers)


def _build_experiments_search_author_filter(
    user_ids: Sequence[str] | None,
    github_usernames: Sequence[str] | None,
    *,
    org_id: str | None,
    emails: Sequence[str] | None = None,
):
    """AND-able EXISTS clause for the ``github:`` search qualifier.

    Sibling of ``_build_experiments_author_filter`` that accepts *iterables*
    (resolved by ``resolve_search_authors``) and unions them with the same
    primary-task attribution precedence the Members dropdown uses. It is
    applied as an additional ``.where()`` so it ANDs with the owner control
    and ``tag:`` filters.

    Unlike the owner filter, the primary-task fallback here is *always* emitted
    and is not gated by the unowned-experiments probe. A handle search routinely
    targets people who are **not** active org members (external GitHub
    contributors), so there is no resolved ``user_id`` to seek on the indexed
    ``owner_user_id`` -- the only way to reach their work is the primary-task
    match. The fallback covers experiments whose owner is NULL **or** the
    ``__unattributed__`` sentinel: the owner backfill stamps that sentinel on
    experiments it can't attribute to any active member (again, the common case
    for external contributors), so gating on ``owner_user_id IS NULL`` alone
    silently dropped every such experiment once the backfill had converged.

    Returns ``false()`` when nothing was resolved so the qualifier narrows to
    an empty result rather than silently disappearing.
    """
    normalized_user_ids = [uid for uid in (user_ids or ()) if uid]
    handles = [
        handle
        for handle in (
            _normalize_github_handle(name) for name in (github_usernames or ())
        )
        if handle
    ]
    normalized_emails = [
        email.strip() for email in (emails or ()) if (email or "").strip()
    ]

    if not normalized_user_ids and not handles and not normalized_emails:
        return false()

    tiers = []
    # Fast path: indexed owner column when the handle resolves to org members.
    if normalized_user_ids:
        tiers.append(ExperimentModel.owner_user_id.in_(normalized_user_ids))

    # Primary-task fallback for experiments not owned by a resolved member --
    # both NULL owners and the ``__unattributed__`` sentinel (see docstring).
    primary_match = _build_primary_task_search_match(
        normalized_user_ids, handles, emails=normalized_emails
    )
    if primary_match is not None:
        primary_task_id = _first_live_task_id_for_experiment()
        legacy_exists = (
            select(1)
            .select_from(TaskModel)
            .where(TaskModel.id == primary_task_id)
            .where(primary_match)
        )
        if org_id is not None:
            legacy_exists = legacy_exists.where(TaskModel.org_id == org_id)
        tiers.append(
            and_(
                or_(
                    ExperimentModel.owner_user_id.is_(None),
                    ExperimentModel.owner_user_id == EXPERIMENTS_UNATTRIBUTED_OWNER,
                ),
                legacy_exists.exists(),
            )
        )

    if not tiers:
        return false()
    return or_(*tiers)


def _experiment_row_passes_status_filter(row, *, status_filter: str) -> bool:
    if status_filter == "active":
        return int(row["active_trials"] or 0) > 0
    if status_filter == "retrying":
        return int(row["retrying_trials"] or 0) > 0
    if status_filter == "needs-review":
        return int(row["verdict_needs_review"] or 0) > 0
    if status_filter == "pending-verdict":
        return int(row["verdict_pending"] or 0) > 0
    if status_filter == "failed":
        return int(row["verdict_failed"] or 0) > 0 or int(row["failed_trials"] or 0) > 0
    if status_filter == "completed":
        return int(row["active_trials"] or 0) == 0
    return True


async def _org_has_unowned_live_experiments(
    session: AsyncSession, org_id: str | None
) -> bool:
    """One indexed probe: does this org still have live experiments with no owner?

    Rides ``idx_experiments_org_owner_user_live``. While any NULL-owner rows
    remain the Mine filter keeps the legacy EXISTS fallback for correctness;
    after the sweep backfill converges this returns False and the filter
    becomes a pure indexed owner_user_id seek.
    """
    probe = (
        select(1)
        .select_from(ExperimentModel)
        .where(ExperimentModel.owner_user_id.is_(None))
        .where(ExperimentModel.deleted_at.is_(None))
        .limit(1)
    )
    if org_id is not None:
        probe = probe.where(ExperimentModel.org_id == org_id)
    return (await session.execute(probe)).first() is not None


def _experiment_tag_assignment_exists(
    tag_ids: list[str], *, param_key: str, negate: bool = False
):
    """(NOT) EXISTS over ACTIVE EXPERIMENT-scope assignments whose
    merge-resolved tag is one of ``tag_ids`` and whose owning tag is alive.
    ``param_key`` must be unique per clause — the clauses are ANDed into one
    statement and identical bind names would collide."""
    prefix = "NOT EXISTS" if negate else "EXISTS"
    return text(
        f"""
        {prefix} (
            SELECT 1
            FROM tag_assignments ta
            JOIN tags t0 ON t0.id = ta.tag_id
            JOIN tags t ON t.id = COALESCE(t0.merged_into_id, t0.id)
            WHERE ta.scope = CAST('EXPERIMENT' AS tag_assignment_scope)
              AND ta.state = 'ACTIVE'
              AND ta.deleted_at IS NULL
              AND ta.target_id = experiments.id
              AND t.deleted_at IS NULL
              AND t.state <> 'DELETED'
              AND t.id = ANY(:{param_key})
        )
        """
    ).bindparams(**{param_key: list(tag_ids)})


def _experiment_tag_predicates(resolved: ResolvedTagFilter) -> list:
    """WHERE clauses for the experiments page query: ``all`` AND-chains one
    EXISTS per id, ``any`` is one EXISTS over the set, ``none`` one NOT
    EXISTS. Resolution (merge-following, DELETED-dropping) happened in
    ``resolve_names_to_ids``; the EXISTS re-checks liveness so a tag deleted
    between resolution and execution can't match."""
    clauses = []
    for n, tid in enumerate(resolved.all_ids):
        clauses.append(
            _experiment_tag_assignment_exists([tid], param_key=f"exp_tags_all_{n}")
        )
    if resolved.any_ids:
        clauses.append(
            _experiment_tag_assignment_exists(
                list(resolved.any_ids), param_key="exp_tags_any"
            )
        )
    if resolved.none_ids:
        clauses.append(
            _experiment_tag_assignment_exists(
                list(resolved.none_ids), param_key="exp_tags_none", negate=True
            )
        )
    return clauses


def _attach_user_tags_to_task_payloads(
    payloads: list[dict], by_task: dict[str, list[UserTagView]]
) -> None:
    """Fill the (already-present, defaulted-empty) ``user_tags`` field on
    dashboard task payloads from effective-tag projection views."""
    for payload in payloads:
        views = by_task.get(str(payload.get("id")), [])
        payload["user_tags"] = [_user_tag_view_payload(v) for v in views]


def _user_tag_view_payload(view: UserTagView) -> dict[str, Any]:
    """JSON shape the frontend's UserTagRef expects."""
    return {
        "tag_id": str(view.tag_id),
        "key": view.key,
        "value": view.value,
        "color": view.color,
        "visibility": view.visibility,
        "current": bool(view.current),
        "older": bool(view.older),
    }


def _has_unknown_positive_tokens(ast: TagFilterAST, unknown: set[str]) -> bool:
    """Unknown ``all``/``any`` tokens can never match — return an empty page
    (graceful type-ahead, mirrors /tasks browse). Unknown ``none`` tokens are
    harmless and ignored. ``unknown`` holds RAW tokens, as the resolver
    reports them."""
    return bool((set(ast.all) | set(ast.any_)) & unknown)


async def load_dashboard_experiments(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    experiments_limit: int,
    experiments_offset: int,
    experiments_query: str | None,
    experiments_status: str,
    experiments_tags: str | None = None,
    experiments_tags_any: str | None = None,
    experiments_tags_none: str | None = None,
    experiments_author_user_id: str | None = None,
    experiments_author_github_usernames: Sequence[str] | None = None,
    experiments_author_emails: Sequence[str] | None = None,
    experiments_search_author_user_ids: Sequence[str] | None = None,
    experiments_search_author_github_usernames: Sequence[str] | None = None,
    experiments_search_author_emails: Sequence[str] | None = None,
    record_timing: TimingRecorder | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Load experiment summaries for the dashboard.

    Two-step query (much faster than the previous full-org aggregation):

    1. Page experiments by the denormalized ``last_activity_at``
       column (indexed via ``idx_experiments_org_last_activity_live``).
       Optional text filter on ``experiment.name`` / ``experiment.id``
       / latest-author fields runs against the same indexed scan.
    2. Aggregate per-experiment task / trial counts only for the page
       ids returned in step 1.

    Status filters can't be applied until after the aggregates exist,
    so when one is set we over-fetch a wider window in step 1 and
    trim post-aggregation. The over-fetch ceiling caps worst-case
    work even on huge orgs.
    """
    if experiments_author_user_id == UNRESOLVED_EXPERIMENTS_OWNER:
        return [], False

    # ------------------------------------------------------------------
    # Step 1: page experiment ids by ``last_activity_at`` (indexed).
    # ------------------------------------------------------------------
    needs_overfetch = experiments_status not in ("", "all")
    page_size = experiments_limit + 1
    if needs_overfetch:
        page_size = min(
            (experiments_limit + 1) * _STATUS_FILTER_OVERFETCH_MULTIPLIER,
            _STATUS_FILTER_OVERFETCH_CEILING,
        )

    normalized_query = (experiments_query or "").strip().lower()

    page_query = select(
        ExperimentModel.id.label("experiment_id"),
        ExperimentModel.name.label("experiment_name"),
        ExperimentModel.is_public.label("experiment_is_public"),
        ExperimentModel.last_activity_at.label("last_activity_at"),
    )
    if org_id is not None:
        page_query = page_query.where(ExperimentModel.org_id == org_id)
    if normalized_query:
        # Author search has to wait until the latest_task lookup runs
        # (step 1.5) -- the index-friendly fields are name and id.
        query_like = f"%{normalized_query}%"
        page_query = page_query.where(
            or_(
                func.lower(ExperimentModel.name).like(query_like),
                func.lower(ExperimentModel.id).like(query_like),
            )
        )
    # Owner filter ("My experiments" / per-member picker): keep only
    # experiments whose primary (oldest) live task belongs to the target author.
    # The ``github:`` search qualifier ANDs an additional author predicate on
    # top; both share the one unowned-experiments probe below.
    # The owner control (Mine / member picker) can drop its primary-task EXISTS
    # fallback once the org has zero NULL owners (pure indexed seek). The github:
    # search filter does NOT share this optimization -- it always needs the
    # primary-task match -- so the probe gates only the owner filter.
    has_search_author = bool(
        (experiments_search_author_user_ids or ())
        or (experiments_search_author_github_usernames or ())
        or (experiments_search_author_emails or ())
    )
    include_legacy_fallback = True
    if experiments_author_user_id is not None:
        probe_started_at = now()
        include_legacy_fallback = await _org_has_unowned_live_experiments(
            session, org_id
        )
        if record_timing is not None:
            record_timing(
                "dashboard_experiments_owner_probe",
                elapsed_ms(probe_started_at),
                "Dashboard unowned-experiments probe",
            )
    author_filter = _build_experiments_author_filter(
        experiments_author_user_id,
        experiments_author_github_usernames,
        org_id=org_id,
        experiments_author_emails=experiments_author_emails,
        include_legacy_fallback=include_legacy_fallback,
    )
    if author_filter is not None:
        page_query = page_query.where(author_filter)
    if has_search_author:
        # ANDs with the owner filter above: with Org selected this is the only
        # author predicate; with Mine selected it intersects (your work AND the
        # searched author's), which is empty unless you are that author.
        search_author_filter = _build_experiments_search_author_filter(
            experiments_search_author_user_ids,
            experiments_search_author_github_usernames,
            org_id=org_id,
            emails=experiments_search_author_emails,
        )
        page_query = page_query.where(search_author_filter)
    tag_ast = TagFilterAST(
        all=[t.strip() for t in (experiments_tags or "").split(",") if t.strip()],
        any_=[t.strip() for t in (experiments_tags_any or "").split(",") if t.strip()],
        none=[t.strip() for t in (experiments_tags_none or "").split(",") if t.strip()],
    )
    if not tag_ast.is_empty():
        resolved, unknown = await resolve_names_to_ids(
            session, org_id=org_id, ast=tag_ast
        )
        if _has_unknown_positive_tokens(tag_ast, unknown):
            return [], False
        for clause in _experiment_tag_predicates(resolved):
            page_query = page_query.where(clause)
    page_query = (
        page_query.order_by(
            nulls_last(ExperimentModel.last_activity_at.desc()),
            ExperimentModel.id.asc(),
        )
        .limit(page_size)
        .offset(experiments_offset)
    )

    page_started_at = now()
    page_rows = (await session.execute(page_query)).mappings().all()
    if record_timing is not None:
        record_timing(
            "dashboard_experiments_page",
            elapsed_ms(page_started_at),
            "Dashboard experiments page lookup",
        )

    if not page_rows:
        return [], False

    experiment_ids = [str(row["experiment_id"]) for row in page_rows]

    user_tags_by_experiment: dict[str, list[UserTagView]] = {}
    try:
        user_tags_by_experiment = await list_direct_tags_for_targets(
            session, scope="EXPERIMENT", target_ids=experiment_ids
        )
    except Exception:  # pragma: no cover - degraded chips beat a dead dashboard
        logger.exception("dashboard experiments user_tags hydration failed")

    # ------------------------------------------------------------------
    # Step 1.5: primary (oldest) and latest task author info for the page.
    # ------------------------------------------------------------------
    task_author_base = (
        select(
            task_experiments.c.experiment_id.label("experiment_id"),
            TaskModel.user.label("task_user"),
            TaskModel.tags["github_username"].astext.label("task_github_username"),
            TaskModel.tags["github_meta"].astext.label("task_github_meta"),
            TaskModel.link.label("task_link"),
        )
        .select_from(
            task_experiments.join(
                TaskModel,  # type: ignore[arg-type]
                TaskModel.id == task_experiments.c.task_id,
            )
        )
        .where(task_experiments.c.experiment_id.in_(experiment_ids))
        .where(task_experiments.c.deleted_at.is_(None))
    )
    if org_id is not None:
        task_author_base = task_author_base.where(TaskModel.org_id == org_id)

    primary_task_query = task_author_base.order_by(
        task_experiments.c.experiment_id.asc(),
        TaskModel.created_at.asc(),
        TaskModel.id.asc(),
    ).distinct(task_experiments.c.experiment_id)

    latest_task_query = task_author_base.order_by(
        task_experiments.c.experiment_id.asc(),
        TaskModel.created_at.desc(),
        TaskModel.id.desc(),
    ).distinct(task_experiments.c.experiment_id)

    primary_task_rows = (await session.execute(primary_task_query)).mappings().all()
    latest_task_rows = (await session.execute(latest_task_query)).mappings().all()
    primary_task_by_id = {str(row["experiment_id"]): row for row in primary_task_rows}
    latest_task_by_id = {str(row["experiment_id"]): row for row in latest_task_rows}

    # ------------------------------------------------------------------
    # Step 2: aggregate task / trial counts for just this page.
    # ------------------------------------------------------------------
    task_agg, trial_agg, score_agg = _build_aggregates_for_experiment_ids(
        experiment_ids, org_id=org_id
    )

    # Iterate the aggregates over the canonical page-id list (via the
    # ``ExperimentModel`` table itself, restricted to the page) so an
    # experiment that has trials but no ``task_experiments`` row still
    # gets its trial counts. Outer-joining off ``task_experiments``
    # would silently drop those.
    agg_query = (
        select(
            ExperimentModel.id.label("experiment_id"),
            func.coalesce(task_agg.c.task_count, 0).label("task_count"),
            func.coalesce(task_agg.c.analysis_tasks, 0).label("analysis_tasks"),
            func.coalesce(task_agg.c.verdict_good, 0).label("verdict_good"),
            func.coalesce(task_agg.c.verdict_needs_review, 0).label(
                "verdict_needs_review"
            ),
            func.coalesce(task_agg.c.verdict_failed, 0).label("verdict_failed"),
            func.coalesce(task_agg.c.verdict_pending, 0).label("verdict_pending"),
            func.coalesce(trial_agg.c.trial_task_count, 0).label("trial_task_count"),
            func.coalesce(trial_agg.c.total_trials, 0).label("total_trials"),
            func.coalesce(trial_agg.c.completed_trials, 0).label("completed_trials"),
            func.coalesce(trial_agg.c.failed_trials, 0).label("failed_trials"),
            func.coalesce(trial_agg.c.retrying_trials, 0).label("retrying_trials"),
            func.coalesce(trial_agg.c.active_trials, 0).label("active_trials"),
            func.coalesce(trial_agg.c.reward_success, 0).label("reward_success"),
            func.coalesce(trial_agg.c.reward_sum, 0.0).label("reward_sum"),
            func.coalesce(trial_agg.c.reward_total, 0).label("reward_total"),
            score_agg.c.avg_score.label("avg_score"),
            task_agg.c.last_task_created_at,
            trial_agg.c.last_trial_created_at,
        )
        .select_from(ExperimentModel)
        .outerjoin(task_agg, task_agg.c.experiment_id == ExperimentModel.id)
        .outerjoin(trial_agg, trial_agg.c.experiment_id == ExperimentModel.id)
        .outerjoin(score_agg, score_agg.c.experiment_id == ExperimentModel.id)
        .where(ExperimentModel.id.in_(experiment_ids))
    )

    agg_started_at = now()
    agg_rows = (await session.execute(agg_query)).mappings().all()
    if record_timing is not None:
        record_timing(
            "dashboard_experiments_aggregate",
            elapsed_ms(agg_started_at),
            "Dashboard experiments aggregate",
        )

    aggregates_by_id = {str(row["experiment_id"]): row for row in agg_rows}

    # ------------------------------------------------------------------
    # Step 3: stitch + post-filter, preserving page order.
    # ------------------------------------------------------------------
    build_started_at = now()
    experiments_response: list[dict[str, Any]] = []
    has_more = False

    for page_row in page_rows:
        if len(experiments_response) >= experiments_limit:
            has_more = True
            break

        exp_id = str(page_row["experiment_id"])
        agg = aggregates_by_id.get(exp_id)
        primary_task = primary_task_by_id.get(exp_id)
        latest_task = latest_task_by_id.get(exp_id)

        # Synthesise zero-valued aggregates when the experiment has no
        # tasks / trials at all, so author-only matches still render.
        merged: dict[str, Any] = {
            "experiment_id": exp_id,
            "experiment_name": page_row["experiment_name"],
            "experiment_is_public": page_row["experiment_is_public"],
            "task_count": int(agg["task_count"]) if agg else 0,
            "analysis_tasks": int(agg["analysis_tasks"]) if agg else 0,
            "verdict_good": int(agg["verdict_good"]) if agg else 0,
            "verdict_needs_review": int(agg["verdict_needs_review"]) if agg else 0,
            "verdict_failed": int(agg["verdict_failed"]) if agg else 0,
            "verdict_pending": int(agg["verdict_pending"]) if agg else 0,
            "total_trials": int(agg["total_trials"]) if agg else 0,
            "completed_trials": int(agg["completed_trials"]) if agg else 0,
            "failed_trials": int(agg["failed_trials"]) if agg else 0,
            "retrying_trials": int(agg["retrying_trials"]) if agg else 0,
            "active_trials": int(agg["active_trials"]) if agg else 0,
            "reward_success": int(agg["reward_success"]) if agg else 0,
            "reward_sum": float(agg["reward_sum"] or 0.0) if agg else 0.0,
            "reward_total": int(agg["reward_total"]) if agg else 0,
            "avg_score": (
                float(agg["avg_score"])
                if agg and agg["avg_score"] is not None
                else None
            ),
            "primary_user": primary_task["task_user"] if primary_task else None,
            "primary_github_username": (
                primary_task["task_github_username"] if primary_task else None
            ),
            "last_user": latest_task["task_user"] if latest_task else None,
            "last_github_username": (
                latest_task["task_github_username"] if latest_task else None
            ),
            "last_github_meta": (
                latest_task["task_github_meta"] if latest_task else None
            ),
            "last_link": latest_task["task_link"] if latest_task else None,
            "user_tags": [
                _user_tag_view_payload(v)
                for v in user_tags_by_experiment.get(exp_id, [])
            ],
        }

        # ``task_count`` mirrors the previous greatest(task, trial) shape
        # so callers see at least the number of tasks linked via trials.
        if agg:
            merged["task_count"] = max(
                int(agg["task_count"] or 0), int(agg["trial_task_count"] or 0)
            )

        # Author-search post-filter: name/id matches already passed in
        # step 1, so any miss here means the user typed an author and
        # neither the primary owner nor the latest runner matched.
        if normalized_query and not (
            normalized_query in str(merged["experiment_name"] or "").lower()
            or normalized_query in exp_id.lower()
            or normalized_query in str(merged["primary_user"] or "").lower()
            or normalized_query in str(merged["primary_github_username"] or "").lower()
            or normalized_query in str(merged["last_user"] or "").lower()
            or normalized_query in str(merged["last_github_username"] or "").lower()
        ):
            continue

        if not _experiment_row_passes_status_filter(
            merged, status_filter=experiments_status
        ):
            continue

        last_created_at = merged.get("last_activity_at") or page_row.get(
            "last_activity_at"
        )
        if agg:
            # Keep the response shape stable: ``last_created_at`` is
            # what the FE renders. Prefer the freshly-aggregated value
            # over ``last_activity_at`` so newly-created tasks show up
            # immediately even before the maintenance pass runs.
            agg_last_task = agg["last_task_created_at"]
            agg_last_trial = agg["last_trial_created_at"]
            candidates = [
                ts
                for ts in (agg_last_task, agg_last_trial, last_created_at)
                if ts is not None
            ]
            last_created_at = max(candidates) if candidates else None

        github_meta = _parse_github_meta(merged["last_github_meta"])
        author = _dashboard_author_from_task(
            github_username=merged["primary_github_username"],
            user=merged["primary_user"],
        )
        last_runner = _dashboard_author_from_task(
            github_username=merged["last_github_username"],
            user=merged["last_user"],
        )

        # The PR URL can arrive two ways: structured ``github_meta.pr_url`` or
        # the canonical ``link`` column (set by ``--link``, or auto-derived from
        # github_meta). ``link`` is what the task/experiment pages render, so we
        # treat it as a first-class fallback rather than relying on github_meta
        # alone. The number is parsed from the URL when github_meta omits it.
        last_pr_url = (
            str(github_meta["pr_url"])
            if github_meta and github_meta.get("pr_url") is not None
            else merged["last_link"]
        )
        if github_meta and github_meta.get("pr_number") is not None:
            last_pr_number = str(github_meta["pr_number"])
        else:
            last_pr_number = _pr_number_from_url(last_pr_url)

        experiments_response.append(
            {
                "id": merged["experiment_id"],
                "name": merged["experiment_name"],
                "is_public": bool(merged["experiment_is_public"]),
                "task_count": int(merged["task_count"] or 0),
                "total_trials": int(merged["total_trials"] or 0),
                "completed_trials": int(merged["completed_trials"] or 0),
                "failed_trials": int(merged["failed_trials"] or 0),
                "retrying_trials": int(merged["retrying_trials"] or 0),
                "active_trials": int(merged["active_trials"] or 0),
                "reward_success": int(merged["reward_success"] or 0),
                "reward_sum": float(merged["reward_sum"] or 0.0),
                "reward_total": int(merged["reward_total"] or 0),
                "avg_score": merged["avg_score"],
                "analysis_tasks": int(merged["analysis_tasks"] or 0),
                "verdict_good": int(merged["verdict_good"] or 0),
                "verdict_needs_review": int(merged["verdict_needs_review"] or 0),
                "verdict_failed": int(merged["verdict_failed"] or 0),
                "verdict_pending": int(merged["verdict_pending"] or 0),
                "last_created_at": (
                    last_created_at.isoformat() if last_created_at else None
                ),
                "author": author,
                "last_runner": last_runner,
                "last_author": last_runner,
                "user_tags": merged.get("user_tags", []),
                "last_pr_url": last_pr_url,
                "last_pr_title": (
                    str(github_meta["pr_title"])
                    if github_meta and github_meta.get("pr_title") is not None
                    else None
                ),
                "last_pr_number": last_pr_number,
            }
        )

    # If we filled the page exactly and the page query returned more
    # rows than we consumed, signal there is more.
    if (
        not has_more
        and len(page_rows) > len(experiments_response)
        and (len(page_rows) >= page_size)
    ):
        has_more = True

    if record_timing is not None:
        record_timing(
            "dashboard_experiments_build",
            elapsed_ms(build_started_at),
            "Dashboard experiments response build",
        )
    return experiments_response, has_more


# ---------------------------------------------------------------------------
# Model usage aggregation
# ---------------------------------------------------------------------------


async def get_model_usage_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    usage_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate per-model cost and token usage from trials."""
    usage_filters = []
    if org_id is not None:
        usage_filters.append(TrialModel.org_id == org_id)
    if usage_minutes is not None:
        since = datetime.now(timezone.utc) - timedelta(minutes=usage_minutes)
        usage_filters.append(TrialModel.created_at >= since)

    usage_query = select(
        TrialModel.model,
        TrialModel.provider,
        func.count(TrialModel.id).label("trial_count"),
        func.sum(TrialModel.input_tokens).label("input_tokens"),
        func.sum(TrialModel.cache_tokens).label("cache_tokens"),
        func.sum(TrialModel.output_tokens).label("output_tokens"),
        func.sum(TrialModel.cost_usd).label("cost_usd"),
        func.count(case((TrialModel.status == TrialStatus.RUNNING, 1))).label(
            "running"
        ),
        func.count(case((TrialModel.status == TrialStatus.RETRYING, 1))).label(
            "retrying"
        ),
        func.count(
            case(
                (
                    TrialModel.status.in_([TrialStatus.PENDING, TrialStatus.QUEUED]),
                    1,
                )
            )
        ).label("queued"),
        func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
            "succeeded"
        ),
        func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label("failed"),
        func.avg(
            case(
                (
                    TrialModel.finished_at.isnot(None),
                    func.extract(
                        "epoch",
                        TrialModel.finished_at - TrialModel.started_at,
                    ),
                )
            )
        ).label("avg_duration_s"),
        func.count(case((TrialModel.finished_at.isnot(None), 1))).label(
            "duration_count"
        ),
    ).group_by(TrialModel.model, TrialModel.provider)
    if usage_filters:
        usage_query = usage_query.where(*usage_filters)

    usage_result = await session.execute(usage_query)
    merged: dict[tuple[str, str], dict[str, int | float | str | None]] = {}
    for row in usage_result.all():
        normalized_provider = (row.provider or "unknown").strip().lower() or "unknown"
        normalized_model = _normalize_dashboard_model(row.model, normalized_provider)
        key = (normalized_model, normalized_provider)
        duration_count = int(row.duration_count or 0)

        if key not in merged:
            merged[key] = {
                "model": normalized_model,
                "provider": normalized_provider,
                "trial_count": 0,
                "input_tokens": 0,
                "cache_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "running": 0,
                "retrying": 0,
                "queued": 0,
                "succeeded": 0,
                "failed": 0,
                "duration_total_s": 0.0,
                "duration_count": 0,
                "avg_duration_s": None,
            }

        agg = merged[key]
        agg["trial_count"] = int(agg["trial_count"]) + int(row.trial_count or 0)
        agg["input_tokens"] = int(agg["input_tokens"]) + int(row.input_tokens or 0)
        agg["cache_tokens"] = int(agg["cache_tokens"]) + int(row.cache_tokens or 0)
        agg["output_tokens"] = int(agg["output_tokens"]) + int(row.output_tokens or 0)
        agg["cost_usd"] = float(agg["cost_usd"]) + float(row.cost_usd or 0)
        agg["running"] = int(agg["running"]) + int(row.running or 0)
        agg["retrying"] = int(agg["retrying"]) + int(row.retrying or 0)
        agg["queued"] = int(agg["queued"]) + int(row.queued or 0)
        agg["succeeded"] = int(agg["succeeded"]) + int(row.succeeded or 0)
        agg["failed"] = int(agg["failed"]) + int(row.failed or 0)
        agg["duration_total_s"] = float(agg["duration_total_s"]) + float(
            (row.avg_duration_s or 0) * duration_count
        )
        agg["duration_count"] = int(agg["duration_count"]) + duration_count

    model_usage: list[dict[str, Any]] = []
    for agg in merged.values():
        dc = int(agg["duration_count"])
        avg_dur = round(float(agg["duration_total_s"]) / dc, 1) if dc > 0 else None
        model_usage.append(
            {
                "model": str(agg["model"]),
                "provider": str(agg["provider"]),
                "trial_count": int(agg["trial_count"]),
                "input_tokens": int(agg["input_tokens"]),
                "cache_tokens": int(agg["cache_tokens"]),
                "output_tokens": int(agg["output_tokens"]),
                "cost_usd": round(float(agg["cost_usd"]), 4),
                "running": int(agg["running"]),
                "retrying": int(agg["retrying"]),
                "queued": int(agg["queued"]),
                "succeeded": int(agg["succeeded"]),
                "failed": int(agg["failed"]),
                "avg_duration_s": avg_dur,
            }
        )
    return model_usage


async def get_worker_job_usage_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    usage_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate job lifecycle usage directly from worker_jobs."""
    filters = []
    if org_id is not None:
        filters.append(WorkerJobModel.org_id == org_id)
    if usage_minutes is not None:
        since = datetime.now(timezone.utc) - timedelta(minutes=usage_minutes)
        filters.append(WorkerJobModel.created_at >= since)

    query = (
        select(
            WorkerJobModel.kind,
            WorkerJobModel.queue_key,
            func.count(WorkerJobModel.id).label("job_count"),
            func.count(
                case((WorkerJobModel.status == WorkerJobStatus.QUEUED, 1))
            ).label("queued"),
            func.count(
                case((WorkerJobModel.status == WorkerJobStatus.RUNNING, 1))
            ).label("running"),
            func.count(
                case((WorkerJobModel.status == WorkerJobStatus.RETRYING, 1))
            ).label("retrying"),
            func.count(
                case((WorkerJobModel.status == WorkerJobStatus.SUCCESS, 1))
            ).label("succeeded"),
            func.count(
                case((WorkerJobModel.status == WorkerJobStatus.FAILED, 1))
            ).label("failed"),
            func.count(
                case((WorkerJobModel.status == WorkerJobStatus.CANCELLED, 1))
            ).label("cancelled"),
            func.count(
                case((WorkerJobModel.status == WorkerJobStatus.BLOCKED, 1))
            ).label("blocked"),
            func.avg(
                case(
                    (
                        WorkerJobModel.finished_at.isnot(None),
                        func.extract(
                            "epoch",
                            WorkerJobModel.finished_at - WorkerJobModel.started_at,
                        ),
                    )
                )
            ).label("avg_duration_s"),
        )
        .group_by(WorkerJobModel.kind, WorkerJobModel.queue_key)
        .order_by(WorkerJobModel.kind, WorkerJobModel.queue_key)
    )
    if filters:
        query = query.where(*filters)

    result = await session.execute(query)
    return [
        {
            "kind": str(row.kind.value),
            "queue_key": str(row.queue_key),
            "job_count": int(row.job_count or 0),
            "queued": int(row.queued or 0),
            "running": int(row.running or 0),
            "retrying": int(row.retrying or 0),
            "succeeded": int(row.succeeded or 0),
            "failed": int(row.failed or 0),
            "cancelled": int(row.cancelled or 0),
            "blocked": int(row.blocked or 0),
            "avg_duration_s": (
                round(float(row.avg_duration_s), 1)
                if row.avg_duration_s is not None
                else None
            ),
        }
        for row in result.all()
    ]


# ---------------------------------------------------------------------------
# Full dashboard core
# ---------------------------------------------------------------------------


async def get_dashboard_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    tasks_limit: int = 200,
    tasks_offset: int = 0,
    experiments_limit: int = 25,
    experiments_offset: int = 0,
    experiments_query: str | None = None,
    experiments_status: str = "all",
    experiments_tags: str | None = None,
    experiments_tags_any: str | None = None,
    experiments_tags_none: str | None = None,
    experiments_author_user_id: str | None = None,
    experiments_author_github_usernames: Sequence[str] | None = None,
    experiments_author_emails: Sequence[str] | None = None,
    experiments_search_author_user_ids: Sequence[str] | None = None,
    experiments_search_author_github_usernames: Sequence[str] | None = None,
    experiments_search_author_emails: Sequence[str] | None = None,
    usage_minutes: int | None = None,
    include_tasks: bool = True,
    include_usage: bool = True,
    include_experiments: bool = True,
    record_timing: TimingRecorder | None = None,
) -> dict:
    """Combined dashboard data: queues, pipeline, usage, tasks, experiments.

    Uses two independent caches so a recent-experiments page change
    doesn't blow away the queue / usage / recent-tasks slice (and vice
    versa). The previous single-cache path forced a full recompute on
    every filter or pagination tweak.
    """

    primary_cache_key = (
        f"dashboard.primary:{org_id}:"
        f"{tasks_limit}:{tasks_offset}:{usage_minutes}:"
        f"{include_tasks}:{include_usage}"
    )
    experiments_cache_key = (
        f"dashboard.experiments:{org_id}:"
        f"{experiments_limit}:{experiments_offset}:{experiments_query}:"
        f"{experiments_status}:{experiments_author_user_id}:"
        f"{','.join(experiments_author_github_usernames or ())}:"
        f"{','.join(experiments_author_emails or ())}:"
        f"{','.join(experiments_search_author_user_ids or ())}:"
        f"{','.join(experiments_search_author_github_usernames or ())}:"
        f"{','.join(experiments_search_author_emails or ())}:"
        f"{experiments_tags}:{experiments_tags_any}:{experiments_tags_none}"
    )

    is_usage_only_request = (
        include_usage and not include_tasks and not include_experiments
    )

    async def _fetch_primary():
        """Queue stats, pipeline stats, usage, and tasks on the caller's session."""
        if is_usage_only_request:
            qs: dict = {}
            ps: dict[str, dict[str, int]] = {
                "trials": {},
                "analyses": {},
                "verdicts": {},
            }
        else:
            # Queue/pipeline stats scan the whole trials table; cache them
            # separately keyed by org_id so the scan doesn't re-run on every
            # task-pagination / usage-window variation of the primary slice.
            queue_cache_key = f"dashboard.queue_pipeline:{org_id}:"
            cached_qp = _slice_get_cached(
                _dashboard_queue_pipeline_cache,
                queue_cache_key,
                _QUEUE_PIPELINE_CACHE_TTL_SECONDS,
            )
            if cached_qp is not None:
                qs, ps = cached_qp
            else:
                queue_started_at = now()
                qs, ps = await get_queue_and_pipeline_stats_with_concurrency(
                    session, org_id
                )
                _slice_set_cached(
                    _dashboard_queue_pipeline_cache, queue_cache_key, (qs, ps)
                )
                if record_timing is not None:
                    record_timing(
                        "dashboard_queue_pipeline",
                        elapsed_ms(queue_started_at),
                        "Queue and pipeline stats",
                    )

        mu: list[dict[str, Any]] = []
        ju: list[dict[str, Any]] = []
        if include_usage:
            usage_started_at = now()
            mu = await get_model_usage_core(
                session, org_id=org_id, usage_minutes=usage_minutes
            )
            ju = await get_worker_job_usage_core(
                session, org_id=org_id, usage_minutes=usage_minutes
            )
            if record_timing is not None:
                record_timing(
                    "dashboard_usage",
                    elapsed_ms(usage_started_at),
                    "Dashboard usage query",
                )

        tr: list[dict] = []
        hm = False
        if include_tasks:
            tasks_q = (
                select(TaskModel)
                .options(selectinload(TaskModel.experiments))
                .order_by(TaskModel.created_at.desc())
                .limit(tasks_limit + 1)
                .offset(tasks_offset)
            )
            if org_id is not None:
                tasks_q = tasks_q.where(TaskModel.org_id == org_id)

            tasks_started_at = now()
            tasks_result = await session.execute(tasks_q)
            if record_timing is not None:
                record_timing(
                    "dashboard_tasks_query",
                    elapsed_ms(tasks_started_at),
                    "Dashboard tasks query",
                )
            paged_tasks = tasks_result.scalars().all()
            hm = len(paged_tasks) > tasks_limit
            fetched_tasks = paged_tasks[:tasks_limit]

            if fetched_tasks:
                build_started_at = now()
                tr = [
                    ts.model_dump()
                    for ts in await build_task_status_responses_from_counts(
                        session, tasks=fetched_tasks
                    )
                ]
                if record_timing is not None:
                    record_timing(
                        "dashboard_tasks_build",
                        elapsed_ms(build_started_at),
                        "Dashboard tasks response build",
                    )
                try:
                    by_task = await list_effective_user_tags_for_task_versions(
                        session, task_ids=[t.id for t in fetched_tasks]
                    )
                    _attach_user_tags_to_task_payloads(tr, by_task)
                except Exception:  # pragma: no cover - chips degrade, dash survives
                    logger.exception("dashboard tasks user_tags hydration failed")

        return {
            "queues": qs,
            "pipeline": ps,
            "model_usage": mu,
            "job_usage": ju,
            "tasks": tr,
            "tasks_limit": tasks_limit,
            "tasks_offset": tasks_offset,
            "has_more": hm,
        }

    async def _fetch_experiments_parallel() -> dict:
        """Experiments on a separate session so they run concurrently with primary."""
        experiments_started_at = now()
        async with get_session() as exp_session:
            response, has_more = await load_dashboard_experiments(
                exp_session,
                org_id=org_id,
                experiments_limit=experiments_limit,
                experiments_offset=experiments_offset,
                experiments_query=experiments_query,
                experiments_status=experiments_status,
                experiments_tags=experiments_tags,
                experiments_tags_any=experiments_tags_any,
                experiments_tags_none=experiments_tags_none,
                experiments_author_user_id=experiments_author_user_id,
                experiments_author_github_usernames=experiments_author_github_usernames,
                experiments_author_emails=experiments_author_emails,
                experiments_search_author_user_ids=experiments_search_author_user_ids,
                experiments_search_author_github_usernames=experiments_search_author_github_usernames,
                experiments_search_author_emails=experiments_search_author_emails,
                record_timing=record_timing,
            )
        if record_timing is not None:
            record_timing(
                "dashboard_experiments_total",
                elapsed_ms(experiments_started_at),
                "Dashboard experiments total",
            )
        return {
            "experiments": response,
            "experiments_limit": experiments_limit,
            "experiments_offset": experiments_offset,
            "experiments_has_more": has_more,
        }

    dashboard_started_at = now()

    primary_cached = _slice_get_cached(
        _dashboard_primary_cache, primary_cache_key, _PRIMARY_CACHE_TTL_SECONDS
    )
    experiments_cached = (
        _slice_get_cached(
            _dashboard_experiments_cache,
            experiments_cache_key,
            _EXPERIMENTS_CACHE_TTL_SECONDS,
        )
        if include_experiments
        else None
    )

    primary_task = (
        asyncio.create_task(_fetch_primary()) if primary_cached is None else None
    )
    experiments_task = (
        asyncio.create_task(_fetch_experiments_parallel())
        if include_experiments and experiments_cached is None
        else None
    )

    if primary_task is not None:
        primary_payload = await primary_task
        _slice_set_cached(_dashboard_primary_cache, primary_cache_key, primary_payload)
    else:
        primary_payload = primary_cached

    if include_experiments:
        if experiments_task is not None:
            experiments_payload = await experiments_task
            _slice_set_cached(
                _dashboard_experiments_cache,
                experiments_cache_key,
                experiments_payload,
            )
        else:
            experiments_payload = experiments_cached
    else:
        experiments_payload = {
            "experiments": [],
            "experiments_limit": experiments_limit,
            "experiments_offset": experiments_offset,
            "experiments_has_more": False,
        }

    response = {
        **primary_payload,
        **experiments_payload,
        "cached": (
            primary_task is None
            and (experiments_task is None or not include_experiments)
        ),
    }

    if record_timing is not None:
        record_timing(
            "dashboard_total",
            elapsed_ms(dashboard_started_at),
            "Dashboard core total",
        )
    return response


# Public aliases: the primary-task attribution helpers are part of the Mine
# owner contract — anything stamping or backfilling experiment owners must
# apply the exact same precedence this filter uses, so they are exported
# rather than copied.
build_primary_task_author_match = _build_primary_task_author_match
first_live_task_id_for_experiment = _first_live_task_id_for_experiment
normalize_github_handle = _normalize_github_handle
