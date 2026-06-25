from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import HTTPException
from sqlalchemy import (
    and_,
    case,
    func,
    nulls_last,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only, selectinload

from oddish.core.helpers import (
    escape_like,
    parse_search_query,
    _parse_github_meta,
    build_task_status_response_compact,
    build_task_status_response,
    build_task_status_responses_from_counts,
    fetch_experiment_effective_version_ids,
    fetch_trial_queue_info,
    fetch_visible_worker_jobs,
    get_task_status_trials,
    resolve_effective_version_id,
)
from oddish.core.tags.filter_ast import (
    TagFilterAST,
    build_filter_predicates,
    resolve_names_to_ids,
)
from oddish.core.tags.projection import (
    list_effective_user_tags_for_task_versions,
)
from oddish.db import (
    ExperimentModel,
    TagModel,
    TagState,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)
from oddish.schemas import (
    TaskBrowseExperiment,
    TaskBrowseItem,
    TaskBrowseResponse,
    TaskBrowseTrial,
    TaskStatusResponse,
    UserTagRef,
)
from oddish.model_pricing import estimate_cost_usd
from oddish.timing import TimingRecorder, elapsed_ms, now


def _resolve_browse_trial_cost(row: Mapping[str, Any]) -> tuple[float | None, bool]:
    """Resolve a single browse trial's cost. Mirrors ``_resolve_trial_cost``:
    prefer the agent's native ``cost_usd``; otherwise token-estimate (CLI
    agents like cursor-cli / gemini-cli report tokens but no native cost).

    Returns ``(cost_usd, is_estimated)``; ``(None, False)`` when unpriceable.
    """
    cost = row["cost_usd"]
    if cost is not None:
        return float(cost), False
    if row["input_tokens"] is None and row["output_tokens"] is None:
        return None, False
    from oddish.config import settings

    model_name = settings.normalize_trial_model(row["agent"], row["model"])
    estimated = estimate_cost_usd(
        model_name or row["model"],
        row["input_tokens"],
        row["output_tokens"],
        row["cache_tokens"],
    )
    if estimated is None:
        return None, False
    return estimated, True


async def list_tasks_core(
    session: AsyncSession,
    *,
    status: str | None = None,
    user: str | None = None,
    experiment_id: str | None = None,
    include_trials: bool = True,
    compact_trials: bool = False,
    compact_tasks: bool = False,
    include_queue_info: bool = True,
    include_worker_jobs: bool = True,
    limit: int = 100,
    offset: int = 0,
    org_id: str | None = None,
    include_empty_rewards: bool = True,
    record_timing: TimingRecorder | None = None,
) -> list[TaskStatusResponse]:
    """List tasks with optional filters and aggregated trial stats.

    ``compact_tasks=True`` is a shortcut path used by the experiment
    page first paint (``limit=2000&include_trials=False``). It drops
    the per-task ``visible_worker_jobs`` fetch, the experiment-scoped
    ``effective_version_ids`` lookup, and the ``selectinload(experiments)``
    fan-out -- none of which are read by the lightweight task-shell view
    that consumes this path. It implies ``include_trials=False``.
    """
    if compact_tasks:
        include_trials = False
        include_worker_jobs = False
    query = select(TaskModel).order_by(TaskModel.created_at.desc())
    if include_trials:
        # When scoped to an experiment, push the trial filter into the
        # selectin load so each task fetches only that experiment's non-probe
        # trials instead of every trial across every version / experiment /
        # superseded rerun. The former code loaded the full set and filtered
        # in Python (below), which materialized far more rows than the view
        # needs -- the memory spike that OOM-killed the API container. This is
        # an exact in-SQL equivalent of that Python filter: ``experiment_id``
        # and ``is_probe`` are both NOT NULL, so ``experiment_id == X``
        # excludes legacy/NULL-experiment trials (``None == X`` is False in
        # Python, ``NULL = X`` is not-true in SQL) and ``is_probe.is_(False)``
        # matches ``not t.is_probe``. The effective-version resolution and the
        # superseded/off-version drop stay in Python, computed from the scoped
        # set exactly as before. The filtered selectin still runs inside the
        # async session (eager, no lazy load -> no MissingGreenlet) and still
        # inherits the soft-delete ``deleted_at IS NULL`` criteria.
        #
        # NOTE: this relies on ``task.trials`` being UNLOADED on the incoming
        # session. A filtered selectin scopes the collection on first load but
        # does NOT re-filter one already fully loaded in the same session. Every
        # ``/tasks`` route calls this on a fresh per-request session, so it holds
        # today; if this helper is ever reused after the full ``trials`` set was
        # loaded on the same session, add ``populate_existing()`` (or re-scope in
        # Python) or the filter will silently not apply.
        if experiment_id:
            trials_relationship = TaskModel.trials.and_(
                TrialModel.experiment_id == experiment_id,
                TrialModel.is_probe.is_(False),
            )
        else:
            trials_relationship = TaskModel.trials
        trials_loader = selectinload(trials_relationship)
        experiments_loader = selectinload(TaskModel.experiments)
        if compact_trials:
            trials_loader = trials_loader.load_only(
                TrialModel.id,
                TrialModel.name,
                TrialModel.task_id,
                TrialModel.task_version_id,
                TrialModel.experiment_id,
                TrialModel.agent,
                TrialModel.provider,
                TrialModel.queue_key,
                TrialModel.model,
                TrialModel.status,
                # ``origin`` is surfaced in compact responses, so it must
                # be loaded eagerly; otherwise the response builder
                # triggers a lazy-load attempt outside the async
                # greenlet and fails with MissingGreenlet.
                TrialModel.origin,
                TrialModel.attempts,
                TrialModel.max_attempts,
                TrialModel.harbor_stage,
                TrialModel.reward,
                TrialModel.error_message,
                # Surfaced by ``build_compact_trial_response`` (probe
                # trials read it on the experiment page). Must be loaded
                # eagerly; otherwise the compact builder triggers a
                # lazy-load on this deferred JSONB column outside the
                # async greenlet and fails with MissingGreenlet (same
                # reason ``origin`` / ``superseded_by_trial_id`` are here).
                TrialModel.harbor_config,
                # Surfaced by both trial builders (``harbor_sha=trial.harbor_sha``).
                # Must be loaded eagerly; otherwise the compact builder triggers a
                # lazy-load on this column outside the async greenlet and fails
                # with MissingGreenlet (same reason ``harbor_config`` is here).
                TrialModel.harbor_sha,
                # Read by the compact builder (``build_compact_trial_response``);
                # the experiment-scoped path also filters on ``is_probe``, but
                # that now happens in SQL via the filtered selectin above. Must
                # still be loaded eagerly for the builder; otherwise accessing
                # it triggers a lazy-load on this column outside the async
                # greenlet and fails with MissingGreenlet (same reason
                # ``origin`` / ``harbor_config`` are here).
                TrialModel.is_probe,
                TrialModel.has_trajectory,
                TrialModel.phase_timing,
                TrialModel.analysis_status,
                # Eagerly load the analysis JSONB on the compact path so
                # ``build_compact_trial_response`` can read
                # ``classification`` / ``subtype`` / ``evidence`` without
                # a follow-up ``fetch_trial_analysis_summaries`` round
                # trip. The blob is small in practice (3 short fields)
                # and skipping the extra query is one of the bigger
                # wins on the experiment-page batched fetch.
                TrialModel.analysis,
                TrialModel.input_tokens,
                TrialModel.cache_tokens,
                TrialModel.output_tokens,
                TrialModel.total_steps,
                TrialModel.cost_usd,
                # Loaded eagerly so the compact builder can surface the
                # rerun pointer without triggering a lazy-load outside
                # the async greenlet (same reason ``origin`` is here).
                TrialModel.superseded_by_trial_id,
                TrialModel.created_at,
                TrialModel.started_at,
                TrialModel.finished_at,
            )
            experiments_loader = experiments_loader.load_only(
                ExperimentModel.id,
                ExperimentModel.name,
                ExperimentModel.is_public,
                ExperimentModel.created_at,
            )
            query = query.options(
                load_only(
                    TaskModel.id,
                    TaskModel.name,
                    TaskModel.status,
                    TaskModel.priority,
                    TaskModel.user,
                    TaskModel.tags,
                    TaskModel.link,
                    TaskModel.task_path,
                    TaskModel.current_version_id,
                    TaskModel.run_analysis,
                    # Surfaced as ``run_probe`` on every task response. Must
                    # be eagerly loaded; otherwise ``_build_task_status_response``
                    # triggers a lazy-load on this deferred column outside the
                    # async greenlet and the whole compact-trials fetch 500s
                    # with MissingGreenlet (same reason ``link`` is here).
                    TaskModel.run_probe,
                    TaskModel.verdict_status,
                    TaskModel.verdict,
                    TaskModel.verdict_error,
                    # Read by the experiment page's PR badge
                    # (``pickExperimentPr``). Must be eagerly loaded; otherwise
                    # the response builder triggers a lazy-load on this deferred
                    # column outside the async greenlet and fails with
                    # MissingGreenlet (same reason ``origin`` is loaded above).
                    TaskModel.link,
                    TaskModel.created_at,
                    TaskModel.started_at,
                    TaskModel.finished_at,
                ),
                trials_loader,
                experiments_loader,
            )
        else:
            query = query.options(trials_loader, experiments_loader)
    else:
        # ``selectinload`` here is one batched round trip even on the
        # compact path -- ``_build_task_status_response`` reads
        # ``task.experiments`` for the primary-experiment lookup. The
        # bigger compact-mode wins are skipping
        # ``fetch_experiment_effective_version_ids`` (an IN-list of up
        # to 2000 task ids) and ``fetch_visible_worker_jobs``.
        query = query.options(selectinload(TaskModel.experiments))

    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    if status:
        query = query.where(TaskModel.status == status)
    if user:
        query = query.where(TaskModel.user == user)
    if experiment_id:
        query = query.where(
            TaskModel.experiments.any(ExperimentModel.id == experiment_id)
        )

    query = query.limit(limit).offset(offset)
    query_started_at = now()
    result = await session.execute(query)
    if record_timing is not None:
        record_timing(
            "tasks_query",
            elapsed_ms(query_started_at),
            "List tasks query",
        )
    tasks = result.scalars().all()

    # When trial payloads are loaded, constrain them to the subset the status UI
    # should reflect: first the requested experiment, then the task's active
    # version within that experiment.  Within an experiment the "active version"
    # is the latest version that has trials in that experiment — not the task's
    # global ``current_version_id`` — so an experiment still shows its own
    # trials after the underlying task is re-uploaded elsewhere.
    if include_trials:
        from sqlalchemy.orm.attributes import set_committed_value

        for task in tasks:
            if experiment_id:
                # ``task.trials`` is already scoped to this experiment's
                # non-probe trials by the filtered selectin load above (probes
                # have their own tab via ``list_experiment_probes_core``, and
                # excluding them before resolving the effective version stops a
                # probe-only version from skewing it). Resolve the experiment's
                # effective version from that scoped set, then drop superseded /
                # off-version trials -- identical result to before, without
                # re-filtering in Python.
                effective = resolve_effective_version_id(
                    task, experiment_context_id=experiment_id
                )
                set_committed_value(
                    task,
                    "trials",
                    get_task_status_trials(task, version_id=effective),
                )
            else:
                set_committed_value(task, "trials", get_task_status_trials(task))

    if include_trials:
        visible_jobs_started_at = now()
        trial_ids = [trial.id for task in tasks for trial in task.trials]
        jobs_by_subject = (
            await fetch_visible_worker_jobs(
                session,
                task_ids=[task.id for task in tasks],
                trial_ids=trial_ids,
            )
            if include_worker_jobs
            else {}
        )
        if record_timing is not None:
            record_timing(
                "tasks_worker_jobs",
                elapsed_ms(visible_jobs_started_at),
                "Visible worker jobs",
            )
        queue_info_started_at = now()
        queue_info_by_trial_id = (
            await fetch_trial_queue_info(
                session,
                trials=[trial for task in tasks for trial in task.trials],
            )
            if include_queue_info
            else {}
        )
        if record_timing is not None:
            record_timing(
                "tasks_queue_info",
                elapsed_ms(queue_info_started_at),
                "Trial queue info",
            )
        if compact_trials:
            # The analysis summary fields (classification / subtype /
            # evidence) are now loaded inline on the trials selectinload
            # via ``TrialModel.analysis`` in the compact load_only set.
            # ``build_compact_trial_response`` falls through to read them
            # from ``trial.analysis`` directly when no
            # ``analysis_summaries`` mapping is passed, so we can skip
            # the extra ``fetch_trial_analysis_summaries`` round trip
            # entirely on this path.
            build_started_at = now()
            response = [
                build_task_status_response_compact(
                    task,
                    include_empty_rewards=include_empty_rewards,
                    queue_info_by_trial_id=queue_info_by_trial_id,
                    jobs_by_subject=jobs_by_subject,
                    experiment_context_id=experiment_id,
                )
                for task in tasks
            ]
            if record_timing is not None:
                record_timing(
                    "tasks_build",
                    elapsed_ms(build_started_at),
                    "Build compact task response",
                )
            return response
        build_started_at = now()
        response = [
            build_task_status_response(
                task,
                include_empty_rewards=include_empty_rewards,
                queue_info_by_trial_id=queue_info_by_trial_id,
                jobs_by_subject=jobs_by_subject,
                experiment_context_id=experiment_id,
            )
            for task in tasks
        ]
        if record_timing is not None:
            record_timing(
                "tasks_build",
                elapsed_ms(build_started_at),
                "Build task response",
            )
        return response

    build_started_at = now()
    effective_version_id_by_task_id: dict[str, str] = {}
    if experiment_id and tasks and not compact_tasks:
        # Skipped on the compact path: the experiment page uses the
        # task version baked into each trial row when it later loads
        # the trial pages, so the lightweight first-paint shell doesn't
        # need this lookup. Phase 4B folds it into the main task list
        # query via a window function for the non-compact path.
        effective_version_id_by_task_id = await fetch_experiment_effective_version_ids(
            session,
            experiment_id=experiment_id,
            task_ids=[task.id for task in tasks],
        )
    response = await build_task_status_responses_from_counts(
        session,
        tasks=tasks,
        include_empty_rewards=include_empty_rewards,
        experiment_context_id=experiment_id,
        effective_version_id_by_task_id=effective_version_id_by_task_id or None,
        jobs_by_subject=(
            await fetch_visible_worker_jobs(
                session,
                task_ids=[task.id for task in tasks],
                trial_ids=[],
            )
            if include_worker_jobs
            else {}
        ),
    )
    if record_timing is not None:
        record_timing(
            "tasks_build",
            elapsed_ms(build_started_at),
            "Build task counts response",
        )
    return response


def _task_freetext_match(needle: str):
    """Broad match for one bare (un-prefixed) browse needle.

    Matches the task name, the author (legacy ``user`` / ``github_username``
    tag), OR a tag name -- so a plain word finds tasks by name, author, or tag
    without the ``github:`` / ``tag:`` prefixes. The needle is literal text;
    ``escape_like`` neutralizes %, _ and backslash.
    """
    pattern = f"%{escape_like(needle)}%"
    tag_name_exists = (
        select(1)
        .select_from(TagModel)
        # tags.id = ANY(tasks.effective_tag_ids) -- the row's current tag set.
        .where(TaskModel.effective_tag_ids.any(TagModel.id))
        .where(TagModel.deleted_at.is_(None))
        .where(TagModel.state != TagState.DELETED)
        .where(TagModel.key.ilike(pattern, escape="\\"))
        .correlate(TaskModel)
        .exists()
    )
    return or_(
        TaskModel.name.ilike(pattern, escape="\\"),
        TaskModel.user.ilike(pattern, escape="\\"),
        TaskModel.tags["github_username"].astext.ilike(pattern, escape="\\"),
        tag_name_exists,
    )


def _build_browse_author_filter(
    user_ids: Sequence[str] | None,
    github_usernames: Sequence[str] | None,
    emails: Sequence[str] | None,
):
    """Direct author predicate for the task browser, or ``None``.

    Every column lives on ``TaskModel`` -- no join needed. The matches are
    case-insensitive on ``lower(tags ->> 'github_username')`` and
    ``lower(user)`` so they ride the existing partial indexes
    ``idx_tasks_org_lower_github_tag_live`` / ``idx_tasks_org_lower_user_live``;
    ``created_by_user_id`` rides ``idx_tasks_org_created_by_live``. The legacy
    ``user`` string can hold either a handle or an email, so it is matched
    against both. Returns ``None`` when no author was supplied (normal browse).
    """
    normalized_user_ids = [uid for uid in (user_ids or ()) if uid]
    lowered_handles = [
        handle
        for handle in (
            (name or "").strip().lstrip("@").lower()
            for name in (github_usernames or ())
        )
        if handle
    ]
    lowered_emails = [
        email
        for email in ((value or "").strip().lower() for value in (emails or ()))
        if email
    ]

    clauses = []
    if normalized_user_ids:
        clauses.append(TaskModel.created_by_user_id.in_(normalized_user_ids))
    if lowered_handles:
        clauses.append(
            func.lower(TaskModel.tags["github_username"].astext).in_(lowered_handles)
        )
    seen_handles = set(lowered_handles)
    user_values = lowered_handles + [e for e in lowered_emails if e not in seen_handles]
    if user_values:
        clauses.append(func.lower(TaskModel.user).in_(user_values))

    if not clauses:
        return None
    return or_(*clauses)


async def browse_tasks_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    limit: int = 25,
    offset: int = 0,
    query: str | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    author_user_ids: Sequence[str] | None = None,
    author_github_usernames: Sequence[str] | None = None,
    author_emails: Sequence[str] | None = None,
    record_timing: TimingRecorder | None = None,
) -> TaskBrowseResponse:
    """List latest-version task summaries for the task browser."""

    current_version = aliased(TaskVersionModel)
    normalized_query = query.strip() if query else None

    ranked_tasks = (
        select(
            TaskModel.id.label("task_id"),
            TaskModel.name.label("name"),
            TaskModel.current_version_id.label("current_version_id"),
            current_version.version.label("current_version"),
            TaskModel.created_at.label("created_at"),
            TaskModel.link.label("link"),
            TaskModel.tags.label("tags"),
            func.row_number()
            .over(
                partition_by=TaskModel.name,
                order_by=(
                    nulls_last(current_version.version.desc()),
                    TaskModel.created_at.desc(),
                    TaskModel.id.desc(),
                ),
            )
            .label("name_rank"),
        )
        .select_from(TaskModel)
        .outerjoin(current_version, current_version.id == TaskModel.current_version_id)
    )
    if org_id is not None:
        ranked_tasks = ranked_tasks.where(TaskModel.org_id == org_id)
    if normalized_query:
        # Free-text grammar (parse_search_query): terms AND'd in any order,
        # "quoted text" matches contiguously, OR makes either side of a group
        # match, a leading - (or NOT) excludes. Each bare needle matches the
        # task name, author (legacy user / github_username tag), OR a tag name
        # (see _task_freetext_match), so users can search without github:/tag:
        # prefixes; the explicit qualifiers stay precise AND filters.
        terms = parse_search_query(normalized_query)
        for group in terms.include:
            ranked_tasks = ranked_tasks.where(
                or_(*(_task_freetext_match(needle) for needle in group))
            )
        for needle in terms.exclude:
            ranked_tasks = ranked_tasks.where(~_task_freetext_match(needle))

    # Resolve tag filters (ids or names) → tag IDs and append AND/OR/NOT
    # predicates over ``tasks.effective_tag_ids``. The predicates reference the
    # ``tasks`` table literally, and ``ranked_tasks`` uses
    # ``select_from(TaskModel)`` (i.e. the ``tasks`` table), so the text
    # predicates are applied here -- before the subquery is materialised.
    # An unknown POSITIVE token (AND/OR) can never match any task, so the
    # result is an empty page rather than an error -- this keeps type-ahead
    # tag filtering in the dashboard search graceful. Unknown tokens in the
    # NOT set exclude nothing and are simply dropped by the resolver.
    if tags_all or tags_any or tags_none:
        ast = TagFilterAST(
            all=list(tags_all or []),
            any_=list(tags_any or []),
            none=list(tags_none or []),
        )
        resolved_filter, unknown_tokens = await resolve_names_to_ids(
            session, org_id=org_id, ast=ast
        )
        if unknown_tokens & ({*ast.all} | {*ast.any_}):
            return TaskBrowseResponse(
                items=[], limit=limit, offset=offset, has_more=False
            )
        if not resolved_filter.is_empty():
            for predicate in build_filter_predicates(resolved_filter):
                ranked_tasks = ranked_tasks.where(predicate)

    # Author filter (the github:/author:/user: qualifier): ANDs with the
    # free-text and tag predicates above. Resolved upstream to matching org
    # members + aliases; an unknown handle resolves to an empty page.
    author_filter = _build_browse_author_filter(
        author_user_ids, author_github_usernames, author_emails
    )
    if author_filter is not None:
        ranked_tasks = ranked_tasks.where(author_filter)

    ranked_tasks_subquery = ranked_tasks.subquery()

    version_counts = (
        select(
            TaskVersionModel.task_id.label("task_id"),
            func.count(TaskVersionModel.id).label("version_count"),
        )
        .group_by(TaskVersionModel.task_id)
        .subquery()
    )

    trial_activity_at = func.greatest(
        func.coalesce(TrialModel.finished_at, TrialModel.created_at),
        func.coalesce(TrialModel.started_at, TrialModel.created_at),
        TrialModel.created_at,
    )
    # The page ORDERING needs only max(activity) per (task, version); the
    # heavy per-task counters are fetched afterwards for just the visible
    # page. Folding them into this org-wide aggregate made every browse page
    # compute eight aggregates over every trial in the org (~65% of page
    # latency at prod volume) to display 25 rows.
    trial_agg_query = select(
        TrialModel.task_id.label("task_id"),
        TrialModel.task_version_id.label("task_version_id"),
        func.max(trial_activity_at).label("last_run_at"),
    ).where(
        TrialModel.superseded_by_trial_id.is_(None),
        # Probes have their own tab; keep them out of the browser's counts
        # and out of last_run_at, which drives the page ordering.
        TrialModel.is_probe.isnot(True),
    )
    if org_id is not None:
        trial_agg_query = trial_agg_query.where(TrialModel.org_id == org_id)
    trial_aggregates = trial_agg_query.group_by(
        TrialModel.task_id, TrialModel.task_version_id
    ).subquery()

    paged_rows = (
        select(
            ranked_tasks_subquery.c.task_id,
            ranked_tasks_subquery.c.name,
            ranked_tasks_subquery.c.current_version,
            ranked_tasks_subquery.c.current_version_id,
            ranked_tasks_subquery.c.link,
            ranked_tasks_subquery.c.tags,
            func.coalesce(version_counts.c.version_count, 0).label("version_count"),
            trial_aggregates.c.last_run_at.label("last_run_at"),
        )
        .select_from(ranked_tasks_subquery)
        .outerjoin(
            version_counts, version_counts.c.task_id == ranked_tasks_subquery.c.task_id
        )
        .outerjoin(
            trial_aggregates,
            and_(
                trial_aggregates.c.task_id == ranked_tasks_subquery.c.task_id,
                trial_aggregates.c.task_version_id
                == ranked_tasks_subquery.c.current_version_id,
            ),
        )
        .where(ranked_tasks_subquery.c.name_rank == 1)
        .order_by(
            # Fresh "never run" tasks should appear near the top of the
            # browser (ordered by upload time), not buried below every
            # real experiment. Fall back to the task's created_at when
            # no trials have finished yet.
            func.coalesce(
                trial_aggregates.c.last_run_at,
                ranked_tasks_subquery.c.created_at,
            ).desc(),
            nulls_last(ranked_tasks_subquery.c.current_version.desc()),
            ranked_tasks_subquery.c.name.asc(),
        )
        .limit(limit + 1)
        .offset(offset)
    )

    page_started_at = now()
    result = await session.execute(paged_rows)
    if record_timing is not None:
        record_timing(
            "browse_page",
            elapsed_ms(page_started_at),
            "Browse tasks page query",
        )
    raw_rows = result.mappings().all()
    has_more = len(raw_rows) > limit
    visible_rows = raw_rows[:limit]

    experiments_by_task: dict[str, list[TaskBrowseExperiment]] = {}
    latest_trials_by_task: dict[str, list[TaskBrowseTrial]] = {}
    counters_by_task: dict[str, dict] = {}
    # Per-task cost rollup (token-estimated for CLI agents that report tokens
    # but no native cost_usd). Folded into the trials loop below to avoid an
    # extra query. Mirrors the task-detail TaskCostTotals fields.
    cost_by_task: dict[str, dict] = {}
    task_version_pairs = [
        (str(row["task_id"]), str(row["current_version_id"]))
        for row in visible_rows
        if row["current_version_id"] is not None
    ]

    if task_version_pairs:
        counters_query = (
            select(
                TrialModel.task_id.label("task_id"),
                func.count(TrialModel.id).label("total_trials"),
                func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
                    "completed_trials"
                ),
                func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
                    "failed_trials"
                ),
                func.count(case((TrialModel.reward == 1, 1))).label("reward_success"),
                func.sum(TrialModel.reward).label("reward_sum"),
                func.count(case((TrialModel.reward.isnot(None), 1))).label(
                    "reward_total"
                ),
            )
            .where(
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.is_probe.isnot(True),
                tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(
                    task_version_pairs
                ),
            )
            .group_by(TrialModel.task_id)
        )
        if org_id is not None:
            counters_query = counters_query.where(TrialModel.org_id == org_id)
        counters_started_at = now()
        counter_rows = await session.execute(counters_query)
        if record_timing is not None:
            record_timing(
                "browse_counters",
                elapsed_ms(counters_started_at),
                "Browse page trial counters",
            )
        counters_by_task = {
            str(row["task_id"]): dict(row) for row in counter_rows.mappings()
        }
        exp_join_condition = [ExperimentModel.id == TrialModel.experiment_id]
        if org_id is not None:
            exp_join_condition.append(ExperimentModel.org_id == org_id)
        exp_query = (
            select(
                TrialModel.task_id.label("task_id"),
                ExperimentModel.id.label("experiment_id"),
                ExperimentModel.name.label("experiment_name"),
            )
            .select_from(TrialModel)
            .join(ExperimentModel, and_(*exp_join_condition))
            .where(
                TrialModel.experiment_id.isnot(None),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.is_probe.isnot(True),
                tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(
                    task_version_pairs
                ),
            )
            .distinct()
            .order_by(
                TrialModel.task_id.asc(),
                ExperimentModel.name.asc(),
                ExperimentModel.id.asc(),
            )
        )
        if org_id is not None:
            exp_query = exp_query.where(TrialModel.org_id == org_id)
        experiments_started_at = now()
        experiment_rows = await session.execute(exp_query)
        if record_timing is not None:
            record_timing(
                "browse_experiments",
                elapsed_ms(experiments_started_at),
                "Browse experiment query",
            )
        for experiment_row in experiment_rows.mappings():
            experiments_by_task.setdefault(str(experiment_row["task_id"]), []).append(
                TaskBrowseExperiment(
                    id=str(experiment_row["experiment_id"]),
                    name=str(experiment_row["experiment_name"]),
                )
            )

        trial_query = (
            select(
                TrialModel.task_id.label("task_id"),
                TrialModel.id.label("trial_id"),
                TrialModel.name.label("trial_name"),
                TrialModel.status.label("trial_status"),
                TrialModel.reward.label("reward"),
                TrialModel.error_message.label("error_message"),
                TrialModel.agent.label("agent"),
                TrialModel.model.label("model"),
                TrialModel.cost_usd.label("cost_usd"),
                TrialModel.input_tokens.label("input_tokens"),
                TrialModel.output_tokens.label("output_tokens"),
                TrialModel.cache_tokens.label("cache_tokens"),
            )
            .where(
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.is_probe.isnot(True),
                tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(
                    task_version_pairs
                ),
            )
            .order_by(
                TrialModel.task_id.asc(),
                TrialModel.created_at.asc(),
                TrialModel.id.asc(),
            )
        )
        if org_id is not None:
            trial_query = trial_query.where(TrialModel.org_id == org_id)
        trials_started_at = now()
        latest_trial_rows = await session.execute(trial_query)
        if record_timing is not None:
            record_timing(
                "browse_trials",
                elapsed_ms(trials_started_at),
                "Browse trials query",
            )
        for trial_row in latest_trial_rows.mappings():
            task_key = str(trial_row["task_id"])
            latest_trials_by_task.setdefault(task_key, []).append(
                TaskBrowseTrial(
                    id=str(trial_row["trial_id"]),
                    name=str(trial_row["trial_name"]),
                    status=trial_row["trial_status"],
                    reward=trial_row["reward"],
                    error_message=trial_row["error_message"],
                )
            )
            resolved_cost, cost_estimated = _resolve_browse_trial_cost(trial_row)
            if resolved_cost is not None:
                agg = cost_by_task.setdefault(
                    task_key,
                    {
                        "cost_usd": 0.0,
                        "cost_trial_count": 0,
                        "cost_has_estimated": False,
                        "cost_has_native": False,
                    },
                )
                agg["cost_usd"] += resolved_cost
                agg["cost_trial_count"] += 1
                if cost_estimated:
                    agg["cost_has_estimated"] = True
                else:
                    agg["cost_has_native"] = True

    # Hydrate effective user tags for each visible task, batched in a
    # single round trip. Used to populate ``TaskBrowseItem.user_tags`` so
    # the browser can render the tag chips alongside the row.
    visible_task_ids = [str(row["task_id"]) for row in visible_rows]
    user_tags_by_task = (
        await list_effective_user_tags_for_task_versions(
            session, task_ids=visible_task_ids, public_only=False
        )
        if visible_task_ids
        else {}
    )

    build_started_at = now()
    response = TaskBrowseResponse(
        items=[
            TaskBrowseItem(
                id=str(row["task_id"]),
                name=str(row["name"]),
                current_version=(
                    int(row["current_version"])
                    if row["current_version"] is not None
                    else None
                ),
                current_version_id=(
                    str(row["current_version_id"])
                    if row["current_version_id"] is not None
                    else None
                ),
                version_count=int(row["version_count"] or 0),
                total_trials=int(
                    counters_by_task.get(str(row["task_id"]), {}).get("total_trials")
                    or 0
                ),
                completed_trials=int(
                    counters_by_task.get(str(row["task_id"]), {}).get(
                        "completed_trials"
                    )
                    or 0
                ),
                failed_trials=int(
                    counters_by_task.get(str(row["task_id"]), {}).get("failed_trials")
                    or 0
                ),
                reward_success=int(
                    counters_by_task.get(str(row["task_id"]), {}).get("reward_success")
                    or 0
                ),
                reward_sum=float(
                    counters_by_task.get(str(row["task_id"]), {}).get("reward_sum")
                    or 0.0
                ),
                reward_total=int(
                    counters_by_task.get(str(row["task_id"]), {}).get("reward_total")
                    or 0
                ),
                last_run_at=row["last_run_at"],
                link=row["link"],
                github_meta=_parse_github_meta(row["tags"]),
                cost_usd=float(
                    cost_by_task.get(str(row["task_id"]), {}).get("cost_usd") or 0.0
                ),
                cost_trial_count=int(
                    cost_by_task.get(str(row["task_id"]), {}).get("cost_trial_count")
                    or 0
                ),
                cost_has_estimated=bool(
                    cost_by_task.get(str(row["task_id"]), {}).get("cost_has_estimated")
                ),
                cost_has_native=bool(
                    cost_by_task.get(str(row["task_id"]), {}).get("cost_has_native")
                ),
                latest_trials=latest_trials_by_task.get(str(row["task_id"]), []),
                experiments=experiments_by_task.get(str(row["task_id"]), []),
                user_tags=[
                    UserTagRef(
                        tag_id=t.tag_id,
                        key=t.key,
                        value=t.value,
                        color=t.color,
                        visibility=t.visibility,
                        current=t.current,
                        older=t.older,
                    )
                    for t in user_tags_by_task.get(str(row["task_id"]), [])
                ],
            )
            for row in visible_rows
        ],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )
    if record_timing is not None:
        record_timing(
            "browse_build",
            elapsed_ms(build_started_at),
            "Build browse response",
        )
    return response


async def get_task_status_core(
    session: AsyncSession,
    *,
    task_id: str,
    include_trials: bool = True,
    include_empty_rewards: bool = True,
    org_id: str | None = None,
) -> TaskStatusResponse:
    """Get task status with optional org scoping."""
    query = select(TaskModel).options(selectinload(TaskModel.experiments))
    if include_trials:
        query = query.options(selectinload(TaskModel.trials))
    query = query.where(TaskModel.id == task_id)
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    result = await session.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if include_trials:
        from sqlalchemy.orm.attributes import set_committed_value

        set_committed_value(task, "trials", get_task_status_trials(task))
        jobs_by_subject = await fetch_visible_worker_jobs(
            session,
            task_ids=[task.id],
            trial_ids=[trial.id for trial in task.trials],
        )
        queue_info_by_trial_id = await fetch_trial_queue_info(
            session, trials=task.trials
        )
        return build_task_status_response(
            task,
            include_empty_rewards=include_empty_rewards,
            queue_info_by_trial_id=queue_info_by_trial_id,
            jobs_by_subject=jobs_by_subject,
        )

    jobs_by_subject = await fetch_visible_worker_jobs(session, task_ids=[task.id])
    return (
        await build_task_status_responses_from_counts(
            session,
            tasks=[task],
            include_empty_rewards=include_empty_rewards,
            jobs_by_subject=jobs_by_subject,
        )
    )[0]
