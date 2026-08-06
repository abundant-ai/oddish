from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import HTTPException
from sqlalchemy import (
    and_,
    case,
    exists,
    extract,
    func,
    nulls_last,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, lazyload, load_only, noload, selectinload

from oddish.core.experiment_membership import (
    gathered_trial_ids_select,
    trial_in_experiment,
)
from oddish.core.helpers import (
    SLIM_TRIAL_RESPONSE_COLUMNS,
    TASK_STATUS_RESPONSE_COLUMNS,
    escape_like,
    parse_search_query,
    _parse_github_meta,
    build_task_status_response_compact,
    build_task_status_response,
    build_task_status_responses_from_counts,
    build_slim_task_status_response,
    fetch_experiment_effective_version_ids,
    fetch_trial_queue_info,
    fetch_visible_worker_jobs,
    filter_probe_trials_for_effective_versions,
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
    task_experiments,
)
from oddish.schemas import (
    AgentModelFacet,
    ExperimentOption,
    ExperimentOptionsResponse,
    TaskBrowseExperiment,
    TaskBrowseFacets,
    TaskBrowseItem,
    TaskBrowseResponse,
    TaskBrowseTrial,
    TaskStatusResponse,
    UserTagRef,
)
from oddish.core.cost_basis import not_combine_copy_filter
from oddish.core.endpoints.qa_cost import get_task_qa_costs, get_trial_qa_costs
from oddish.filters.trial_metrics import TrialMetricFilter
from oddish.filters.trial_predicates import (
    EligibleTrialScope,
    build_trial_metric_predicate,
)
from oddish.model_pricing import estimate_cost_usd, get_model_pricing
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
    if (
        row["input_tokens"] is None
        and row["output_tokens"] is None
        and not row.get("cache_write_tokens")
    ):
        return None, False
    from oddish.config import settings

    model_name = settings.normalize_trial_model(
        row["agent"], row["model"], strict=False
    )
    estimated = estimate_cost_usd(
        model_name or row["model"],
        row["input_tokens"],
        row["output_tokens"],
        row["cache_tokens"],
        row.get("cache_write_tokens"),
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
                trial_in_experiment(experiment_id),
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
                TrialModel.environment,
                TrialModel.status,
                TrialModel.origin,
                TrialModel.attempts,
                TrialModel.max_attempts,
                TrialModel.harbor_stage,
                TrialModel.reward,
                TrialModel.error_message,
                TrialModel.harbor_config,
                TrialModel.harbor_sha,
                TrialModel.is_probe,
                TrialModel.has_trajectory,
                TrialModel.phase_timing,
                TrialModel.analysis_status,
                TrialModel.analysis,
                TrialModel.analysis_started_at,
                TrialModel.analysis_finished_at,
                TrialModel.input_tokens,
                TrialModel.cache_tokens,
                TrialModel.cache_write_tokens,
                TrialModel.output_tokens,
                TrialModel.total_steps,
                TrialModel.trajectory_duration_seconds,
                TrialModel.total_tool_calls,
                TrialModel.tool_counts,
                TrialModel.cost_usd,
                TrialModel.billed_user_id,
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
                ExperimentModel.owner,
                ExperimentModel.link,
            )
            query = query.options(
                load_only(*TASK_STATUS_RESPONSE_COLUMNS),
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
    # version within that experiment. The task's explicit default wins when the
    # experiment has visible trials for it; otherwise the latest represented
    # version wins so an experiment still shows its own historical trials after
    # the task's default changes elsewhere.
    # Collection experiments gather trials additively via ``experiment_trials``
    # without rewriting each trial's scalar ``experiment_id``. Compute that
    # gathered set once so effective-version resolution recognizes those trials
    # (otherwise a gathered trial on an older version is loaded, then
    # double-filtered away by the effective-version drop). Empty for a normal
    # experiment -> every path stays identical to before.
    gathered_trial_ids: set[str] = set()
    if experiment_id:
        gathered_trial_ids = set(
            (await session.execute(gathered_trial_ids_select(experiment_id)))
            .scalars()
            .all()
        )

    if include_trials:
        from sqlalchemy.orm.attributes import set_committed_value

        effective_by_task: dict[str, str | None] = {}
        for task in tasks:
            if experiment_id:
                # ``task.trials`` is already scoped to this experiment's
                # non-probe trials by the filtered selectin load above (probes
                # are loaded separately by version and merged into task.trials
                # below, so excluding them here stops a probe-only version from
                # skewing the effective version resolution). Resolve the
                # experiment's effective version from that scoped set, then drop
                # superseded / off-version trials.
                effective = resolve_effective_version_id(
                    task,
                    experiment_context_id=experiment_id,
                    gathered_trial_ids=gathered_trial_ids,
                )
                effective_by_task[task.id] = effective
                set_committed_value(
                    task,
                    "trials",
                    get_task_status_trials(task, version_id=effective),
                )
            else:
                set_committed_value(task, "trials", get_task_status_trials(task))

        # Probe trials are not loaded by the experiment selectin (it filters
        # ``is_probe.is_(False)``). Surface them as their own "Probe" group by
        # batch-loading every task's probes and keeping only those on the same
        # effective version the matrix displays -- ``experiment_id`` is ignored
        # so cross-experiment probes on that version still show. Loaded with
        # full columns (probe volume per task is tiny), so the response builder
        # never lazy-loads -> no MissingGreenlet.
        if experiment_id and tasks:
            task_ids = [task.id for task in tasks]
            probe_stmt = select(TrialModel).where(
                TrialModel.task_id.in_(task_ids),
                TrialModel.is_probe.is_(True),
                TrialModel.superseded_by_trial_id.is_(None),
            )
            if org_id is not None:
                probe_stmt = probe_stmt.where(TrialModel.org_id == org_id)
            probe_rows = (await session.execute(probe_stmt)).scalars().all()
            probes_by_task = filter_probe_trials_for_effective_versions(
                probe_rows, effective_by_task
            )
            for task in tasks:
                extra = probes_by_task.get(task.id)
                if extra:
                    set_committed_value(task, "trials", [*task.trials, *extra])

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
                    gathered_trial_ids=gathered_trial_ids,
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
                gathered_trial_ids=gathered_trial_ids,
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


async def _experiment_member_task_ids(session: AsyncSession, experiment_id: str):
    """Task ids in ``experiment_id``, mirroring ``TaskModel.experiments`` membership.

    Materialized up front so the task fetch filters by primary key instead of an
    ``EXISTS`` probe per row: under ``ORDER BY created_at DESC LIMIT`` that probe
    made Postgres walk the org's whole task set, since members are a small
    fraction and the limit never fills.
    """
    result = await session.execute(
        select(task_experiments.c.task_id)
        .where(task_experiments.c.experiment_id == experiment_id)
        .where(task_experiments.c.deleted_at.is_(None))
    )
    return result.scalars().all()


async def list_experiment_task_shells_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
    limit: int = 2000,
    offset: int = 0,
    include_empty_rewards: bool = True,
    record_timing: TimingRecorder | None = None,
) -> list[TaskStatusResponse]:
    """List task shells for the experiment detail first paint."""
    query_started_at = now()
    member_task_ids = await _experiment_member_task_ids(session, experiment_id)
    query = (
        select(TaskModel)
        .where(TaskModel.id.in_(member_task_ids))
        .order_by(TaskModel.created_at.desc())
        .options(
            load_only(*TASK_STATUS_RESPONSE_COLUMNS),
            # ``TaskModel.trials`` and ``TaskModel.experiments`` default to
            # select-in eager loading.  A task-shell response deliberately
            # contains neither relationship: scoped counts and the effective
            # version are fetched by the aggregate queries below, and the one
            # context experiment is attached explicitly after this query.
            # Suppress both default loaders here so a large collection does
            # not hydrate every historical trial/experiment for its tasks
            # before returning the lightweight shell.
            # Keep trials unloaded (rather than committing an empty
            # collection) so another explicit selectinload in the same
            # request/session can still enrich these task identities.
            lazyload(TaskModel.trials),
            noload(TaskModel.experiments),
        )
    )
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    query = query.limit(limit).offset(offset)

    result = await session.execute(query)
    if record_timing is not None:
        record_timing("tasks_query", elapsed_ms(query_started_at), "Task shells query")
    tasks = result.scalars().all()

    # Attach only the context experiment -- no fan-out. ``set_committed_value``
    # marks the collection loaded so the response builder never triggers a lazy
    # load outside the async greenlet.
    if tasks:
        from sqlalchemy.orm.attributes import set_committed_value

        context_experiment = await session.get(ExperimentModel, experiment_id)
        scoped_experiments = [context_experiment] if context_experiment else []
        for task in tasks:
            set_committed_value(task, "experiments", scoped_experiments)

    build_started_at = now()
    effective_version_id_by_task_id = (
        await fetch_experiment_effective_version_ids(
            session,
            experiment_id=experiment_id,
            task_ids=[task.id for task in tasks],
        )
        if tasks
        else {}
    )
    response = await build_task_status_responses_from_counts(
        session,
        tasks=tasks,
        include_empty_rewards=include_empty_rewards,
        experiment_context_id=experiment_id,
        effective_version_id_by_task_id=(effective_version_id_by_task_id or None),
        jobs_by_subject={},
    )
    if record_timing is not None:
        record_timing("tasks_build", elapsed_ms(build_started_at), "Build task shells")
    return response


async def list_experiment_slim_tasks(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
    limit: int = 2000,
    offset: int = 0,
    include_empty_rewards: bool = True,
    record_timing: TimingRecorder | None = None,
) -> list[TaskStatusResponse]:
    """List slim per-trial grid data for the experiment page."""
    from sqlalchemy.orm.attributes import set_committed_value

    trials_relationship = TaskModel.trials.and_(
        trial_in_experiment(experiment_id),
        TrialModel.is_probe.is_(False),
    )
    query_started_at = now()
    member_task_ids = await _experiment_member_task_ids(session, experiment_id)
    query = (
        select(TaskModel)
        .where(TaskModel.id.in_(member_task_ids))
        .order_by(TaskModel.created_at.desc())
        .options(
            load_only(*TASK_STATUS_RESPONSE_COLUMNS),
            selectinload(trials_relationship).load_only(*SLIM_TRIAL_RESPONSE_COLUMNS),
        )
    )
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    query = query.limit(limit).offset(offset)

    result = await session.execute(query)
    if record_timing is not None:
        record_timing("tasks_query", elapsed_ms(query_started_at), "Slim tasks query")
    tasks = result.scalars().all()

    if tasks:
        context_experiment = await session.get(ExperimentModel, experiment_id)
        scoped_experiments = [context_experiment] if context_experiment else []
        for task in tasks:
            set_committed_value(task, "experiments", scoped_experiments)

    # Trials gathered into a collection carry their home experiment's scalar
    # ``experiment_id``; fold them in so the builder's auto-resolve keeps them
    # at their own (possibly older) version instead of dropping them.
    gathered_trial_ids = set(
        (await session.execute(gathered_trial_ids_select(experiment_id)))
        .scalars()
        .all()
    )

    # One query for the whole page's trials, not one per trial: this is the
    # grid's per-trial QA sidecar, and the grid can page thousands of trials.
    page_trial_ids = [trial.id for task in tasks for trial in task.trials]
    qa_costs_by_trial_id = await get_trial_qa_costs(
        session, trial_ids=page_trial_ids, org_id=org_id
    )

    build_started_at = now()
    response = [
        build_slim_task_status_response(
            task,
            include_empty_rewards=include_empty_rewards,
            experiment_context_id=experiment_id,
            gathered_trial_ids=gathered_trial_ids,
            qa_costs_by_trial_id=qa_costs_by_trial_id,
        )
        for task in tasks
    ]
    if record_timing is not None:
        record_timing("tasks_build", elapsed_ms(build_started_at), "Build slim tasks")
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


# --- Phase 1.2-lite aggregate metrics (no migration) -----------------------
# Everything below is computed ON THE FLY over the scoped trial set (current
# version, non-probe, non-superseded) with NO supporting index or denormalized
# roll-up. This is the deliberately-slow "works now" path; full Phase 1.2
# denormalizes these into task columns + a backfill + indexes so the same filter
# surface becomes cheap. Do not assume any of these predicates is free.

# Substrings that mark an agent timeout in ``error_message`` — kept in lockstep
# with the frontend ``getMatrixStatus`` (see status-config.ts): a timeout that
# still produced a reward is NOT a harness error, it scores normally.
_AGENT_TIMEOUT_LIKE = ("%AgentTimeoutError%", "%Agent execution timed out%")


def _trial_bucket_label() -> Any:
    """SQL bucket (``skipped``/``pass``/``partial``/``fail``/``harness``/
    ``scoreless``/``other``) for one trial, mirroring the frontend
    ``getMatrixStatus`` (oddish/frontend/src/lib/status-config.ts) EXACTLY so the
    browser's Pass/Partial/Fail/Harness count filters agree with the card chips:

      * ``SKIPPED`` status is ``skipped`` — checked FIRST, before the
        error-message rule, because a gate-skipped trial carries
        ``GATE_SKIP_MESSAGE`` and must not be miscounted as ``harness`` (matches
        ``getMatrixStatus``, which returns "skipped" before its error-message
        short-circuit);
      * an ``error_message`` forces ``harness`` — UNLESS it is an agent timeout
        that still produced a reward (then it scores normally);
      * ``FAILED`` status is ``harness`` (same agent-timeout-with-reward carve-out);
      * ``SUCCESS`` status is the reward bucket (``pass`` reward==1 / ``fail``
        reward==0 / ``partial`` otherwise) when a reward exists, else ``scoreless``;
      * anything else (pending/queued/running) is ``other`` and stays uncounted.

    Keep this in lockstep with ``getMatrixStatus``; the AgentTimeout agreement
    test in ``test_browse_filters.py`` guards the carve-out.
    """
    is_agent_timeout = and_(
        TrialModel.error_message.isnot(None),
        or_(*(TrialModel.error_message.like(p) for p in _AGENT_TIMEOUT_LIKE)),
    )
    has_reward = TrialModel.reward.isnot(None)
    reward_bucket: Any = case(
        (TrialModel.reward == 1, "pass"),
        (TrialModel.reward == 0, "fail"),
        else_="partial",
    )
    return case(
        # Checked before the error-message rule: a gate-skipped trial carries an
        # error_message but is its own bucket, not a harness error.
        (TrialModel.status == TrialStatus.SKIPPED, "skipped"),
        (
            and_(
                TrialModel.error_message.isnot(None),
                ~and_(is_agent_timeout, has_reward),
            ),
            "harness",
        ),
        (
            TrialModel.status == TrialStatus.FAILED,
            case(
                (and_(is_agent_timeout, has_reward), reward_bucket),
                else_="harness",
            ),
        ),
        (
            TrialModel.status == TrialStatus.SUCCESS,
            case((has_reward, reward_bucket), else_="scoreless"),
        ),
        else_="other",
    )


def _trial_runtime_seconds() -> Any:
    """Wall-clock seconds for one trial (``finished_at − started_at``), NULL when
    either timestamp is missing so SUM/AVG/MIN ignore unfinished trials. Shared by
    the aggregate metrics and the Phase 2.1 agent/model comparison."""
    return case(
        (
            and_(
                TrialModel.finished_at.isnot(None),
                TrialModel.started_at.isnot(None),
            ),
            extract("epoch", TrialModel.finished_at - TrialModel.started_at),
        ),
        else_=None,
    )


def _trial_total_tokens(*, null_when_all_missing: bool = False) -> Any:
    """Input+output+cache tokens for one trial. By default missing components
    count as 0 (matches the aggregate sum). With ``null_when_all_missing`` the
    whole expression is NULL when the trial reports no tokens at all, so MIN/AVG
    comparisons ignore token-less trials instead of treating them as 0 tokens."""
    total = (
        func.coalesce(TrialModel.input_tokens, 0)
        + func.coalesce(TrialModel.output_tokens, 0)
        + func.coalesce(TrialModel.cache_tokens, 0)
    )
    if not null_when_all_missing:
        return total
    return case(
        (
            or_(
                TrialModel.input_tokens.isnot(None),
                TrialModel.output_tokens.isnot(None),
                TrialModel.cache_tokens.isnot(None),
            ),
            total,
        ),
        else_=None,
    )


# --- Phase 2.1 agent/model comparison (no migration) -----------------------
# "Beats" direction per metric: reward is higher-better; run time, tokens and
# steps are lower-better. Everything is computed on the fly over the same scoped
# trial set the aggregate filters use; denormalize in full Phase 2.
_COMPARE_METRIC_HIGHER_BETTER: dict[str, bool] = {
    "reward": True,
    "runtime": False,
    "tokens": False,
    "steps": False,
    "pass_rate": True,
}
# Per-trial metrics (reduced via best/avg/median). ``pass_rate`` is NOT here: it
# is already a per-subject ratio, so the agg toggle doesn't apply to it.
_PER_TRIAL_METRICS = frozenset({"reward", "runtime", "tokens", "steps"})


def _compare_metric_expr(metric: str) -> Any | None:
    """Per-trial value for a comparison metric, or ``None`` for an unknown key."""
    if metric == "reward":
        return TrialModel.reward
    if metric == "runtime":
        return _trial_runtime_seconds()
    if metric == "tokens":
        return _trial_total_tokens(null_when_all_missing=True)
    if metric == "steps":
        return TrialModel.total_steps
    return None


def _compare_subject_column(compare_by: str) -> Any | None:
    """The trial column the comparison groups on — agent or model."""
    if compare_by == "agent":
        return TrialModel.agent
    if compare_by == "model":
        return TrialModel.model
    return None


def _subject_metric_value(subject_col: Any, value: str, metric: str, agg: str) -> Any:
    """Reduce ONE subject's (agent/model) trials to a single number for a metric.

    Scoped to that subject via ``CASE`` so a task missing the subject yields NULL
    (and drops out of the comparison). Handles:

    * ``pass_rate`` — pass-bucket count ÷ the subject's trials, as a percent
      (0-100); the ``agg`` toggle does not apply.
    * per-trial metrics (reward/runtime/tokens/steps) reduced by ``best`` (MAX for
      higher-better, MIN otherwise), ``avg``, or ``median`` (``percentile_cont``).
    """
    matches = subject_col == value
    if metric == "pass_rate":
        pass_ct = func.count(case((and_(matches, _trial_bucket_label() == "pass"), 1)))
        total_ct = func.count(case((matches, 1)))
        return pass_ct * 100.0 / func.nullif(total_ct, 0)
    scoped: Any = case((matches, _compare_metric_expr(metric)), else_=None)
    if agg == "median":
        return func.percentile_cont(0.5).within_group(scoped)
    if agg == "avg":
        return func.avg(scoped)
    return (
        func.max(scoped)
        if _COMPARE_METRIC_HIGHER_BETTER.get(metric, True)
        else func.min(scoped)
    )


def _compare_threshold(
    b_val: Any, margin: float | None, unit: str | None, higher: bool
) -> Any:
    """B's value adjusted by an optional margin (percent of B or absolute)."""
    if margin is None or margin <= 0:
        return b_val
    if unit == "abs":
        return b_val + margin if higher else b_val - margin
    factor = 1 + margin / 100.0 if higher else 1 - margin / 100.0
    return b_val * factor


def _agent_compare_subquery(
    org_id: str | None,
    *,
    subject_col: Any,
    value_a: str,
    value_b: str,
    metric: str,
    agg: str,
) -> Any:
    """Per-``(task, version)`` values for two subjects (agents or models) on one
    metric, so the GLOBAL "A beats B" filter can join + compare. Same scoped
    trial set as ``_task_metrics_subquery`` (current version via the later join,
    non-probe, non-superseded). On-the-fly; denormalize in full Phase 2."""
    a_val = _subject_metric_value(subject_col, value_a, metric, agg).label("a_val")
    b_val = _subject_metric_value(subject_col, value_b, metric, agg).label("b_val")
    stmt = select(
        TrialModel.task_id.label("task_id"),
        TrialModel.task_version_id.label("task_version_id"),
        a_val,
        b_val,
    ).where(
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.is_probe.isnot(True),
        # Same scoped trial set as ``_task_metrics_subquery`` — exclude combine
        # copies so the "A beats B" comparison isn't skewed by re-materialized
        # duplicates of a subject's trials.
        not_combine_copy_filter(),
        # NOTE: skipped trials are intentionally INCLUDED in metric denominators
        # (a non-pass, like a harness error), so pass_rate reflects "N launched".
    )
    if org_id is not None:
        stmt = stmt.where(TrialModel.org_id == org_id)
    return stmt.group_by(TrialModel.task_id, TrialModel.task_version_id).subquery()


def _subject_value_scalar(
    org_id: str | None, subject_col: Any, value: str, metric: str, agg: str
) -> Any:
    """A CORRELATED scalar subquery for one subject's metric value on the current
    task (``TaskModel``). Used to express "A beats B" as a self-contained boolean
    predicate that composes inside an OR-group (Phase 2.3). Two of these compared
    is slower than the global join, but needs no named join — the price of being
    composable."""
    stmt = select(_subject_metric_value(subject_col, value, metric, agg)).where(
        TrialModel.task_id == TaskModel.id,
        TrialModel.task_version_id == TaskModel.current_version_id,
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.is_probe.isnot(True),
        not_combine_copy_filter(),
    )
    if org_id is not None:
        stmt = stmt.where(TrialModel.org_id == org_id)
    return stmt.correlate(TaskModel).scalar_subquery()


def _compare_predicate_correlated(
    org_id: str | None,
    *,
    subject_col: Any,
    value_a: str,
    value_b: str,
    metric: str,
    agg: str,
    margin: float | None,
    margin_unit: str | None,
) -> Any:
    """ "A beats B" as a boolean predicate over correlated subqueries (for groups)."""
    higher = _COMPARE_METRIC_HIGHER_BETTER.get(metric, True)
    a_val = _subject_value_scalar(org_id, subject_col, value_a, metric, agg)
    b_val = _subject_value_scalar(org_id, subject_col, value_b, metric, agg)
    threshold = _compare_threshold(b_val, margin, margin_unit, higher)
    return a_val > threshold if higher else a_val < threshold


def _top_performer_predicate(
    org_id: str | None, *, subject_col: Any, value: str, metric: str
) -> Any:
    """ "``value`` is the top subject on ``metric`` for the task" — argmax
    (higher-better) / argmin (lower-better) across ALL subjects. Built from a
    per-``(task, version, subject)`` best-value subquery plus a window extreme
    over the task; ties all count. Returns an EXISTS predicate over ``TaskModel``.
    On-the-fly; no migration."""
    higher = _COMPARE_METRIC_HIGHER_BETTER.get(metric, True)
    # Per-subject best value (no CASE needed — we group by the subject column).
    if metric == "pass_rate":
        best_val = (
            func.count(case((_trial_bucket_label() == "pass", 1)))
            * 100.0
            / func.nullif(func.count(TrialModel.id), 0)
        )
    else:
        metric_expr = _compare_metric_expr(metric)
        best_val = func.max(metric_expr) if higher else func.min(metric_expr)
    per_subject_stmt = select(
        TrialModel.task_id.label("task_id"),
        TrialModel.task_version_id.label("task_version_id"),
        subject_col.label("subject"),
        best_val.label("val"),
    ).where(
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.is_probe.isnot(True),
        not_combine_copy_filter(),
        subject_col.isnot(None),
    )
    if org_id is not None:
        per_subject_stmt = per_subject_stmt.where(TrialModel.org_id == org_id)
    per_subject = per_subject_stmt.group_by(
        TrialModel.task_id, TrialModel.task_version_id, subject_col
    ).subquery()

    extreme = (func.max if higher else func.min)(per_subject.c.val).over(
        partition_by=[per_subject.c.task_id, per_subject.c.task_version_id]
    )
    ranked = select(
        per_subject.c.task_id,
        per_subject.c.task_version_id,
        per_subject.c.subject,
        per_subject.c.val,
        extreme.label("task_extreme"),
    ).subquery()
    winners = (
        select(ranked.c.task_id, ranked.c.task_version_id)
        .where(
            ranked.c.subject == value,
            ranked.c.val == ranked.c.task_extreme,
        )
        .subquery()
    )
    return exists(
        select(winners.c.task_id).where(
            winners.c.task_id == TaskModel.id,
            winners.c.task_version_id == TaskModel.current_version_id,
        )
    )


def _trial_cost_sort_expression(models: Sequence[str] | None) -> Any:
    nonnegative_input = func.greatest(func.coalesce(TrialModel.input_tokens, 0), 0)
    nonnegative_output = func.greatest(func.coalesce(TrialModel.output_tokens, 0), 0)
    nonnegative_cache = func.greatest(func.coalesce(TrialModel.cache_tokens, 0), 0)
    nonnegative_cache_write = func.greatest(
        func.coalesce(TrialModel.cache_write_tokens, 0), 0
    )
    uncached_input = func.greatest(
        nonnegative_input - nonnegative_cache - nonnegative_cache_write, 0
    )
    has_estimatable_tokens = or_(
        nonnegative_input > 0,
        nonnegative_output > 0,
        nonnegative_cache_write > 0,
    )
    estimated_by_model = []
    for model in models or ():
        pricing = get_model_pricing(model)
        if pricing is None:
            continue
        cache_read_rate = (
            pricing.cache_read if pricing.cache_read is not None else pricing.input
        )
        cache_write_rate = (
            pricing.cache_write
            if pricing.cache_write is not None
            else pricing.input * 1.25
        )
        estimated_by_model.append(
            (
                and_(TrialModel.model == model, has_estimatable_tokens),
                uncached_input * pricing.input
                + nonnegative_cache * cache_read_rate
                + nonnegative_cache_write * cache_write_rate
                + nonnegative_output * pricing.output,
            )
        )
    return case(
        (TrialModel.cost_usd.isnot(None), TrialModel.cost_usd),
        *estimated_by_model,
        else_=0.0,
    )


def _task_metrics_subquery(
    org_id: str | None,
    *,
    cost_models: Sequence[str] | None = None,
    cost_finished_after: datetime | None = None,
    cost_finished_before: datetime | None = None,
) -> Any:
    """One GROUP BY over the scoped trials, aggregated per (task, version).

    Grouped by ``(task_id, task_version_id)`` and joined later on the task's
    ``current_version_id`` so the aggregates match the same trial set the direct
    ``_trial_exists`` filters use (non-probe, non-superseded, current version).
    Every column is an on-the-fly computation (see the module note above).
    """
    total_tokens = _trial_total_tokens()
    # Wall-clock seconds, NULL when a timestamp is missing so SUM/AVG ignore
    # unfinished / not-yet-started trials (shared with the Phase 2.1 comparison).
    runtime_seconds = _trial_runtime_seconds()
    bucket = _trial_bucket_label()
    cost_scope = []
    if cost_models:
        cost_scope.append(TrialModel.model.in_(list(cost_models)))
    if cost_finished_after is not None:
        cost_scope.append(TrialModel.finished_at >= cost_finished_after)
    if cost_finished_before is not None:
        cost_scope.append(TrialModel.finished_at <= cost_finished_before)
    trial_cost = _trial_cost_sort_expression(cost_models)
    scoped_trial_cost = (
        case((and_(*cost_scope), trial_cost), else_=0.0) if cost_scope else trial_cost
    )
    stmt = select(
        TrialModel.task_id.label("task_id"),
        TrialModel.task_version_id.label("task_version_id"),
        func.avg(TrialModel.reward).label("avg_reward"),
        func.sum(total_tokens).label("total_tokens"),
        func.sum(scoped_trial_cost).label("cost_usd"),
        func.count(TrialModel.id).label("total_trials"),
        func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
            "completed_trials"
        ),
        func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
            "failed_trials"
        ),
        func.count(case((bucket == "pass", 1))).label("pass_count"),
        func.count(case((bucket == "partial", 1))).label("partial_count"),
        func.count(case((bucket == "fail", 1))).label("fail_count"),
        func.count(case((bucket == "harness", 1))).label("harness_count"),
        # Pass rate = pass-bucket count ÷ scoped trials, as a percent (0-100).
        (
            func.count(case((bucket == "pass", 1)))
            * 100.0
            / func.nullif(func.count(TrialModel.id), 0)
        ).label("pass_rate"),
        func.sum(runtime_seconds).label("runtime_total"),
        func.avg(runtime_seconds).label("runtime_avg"),
    ).where(
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.is_probe.isnot(True),
        # Combine copies re-materialize an existing execution under another
        # experiment; excluding them keeps the cost sort / aggregate filters on
        # the same execution population the card counters and detail view show.
        not_combine_copy_filter(),
        # NOTE: skipped trials are intentionally INCLUDED in metric denominators
        # (a non-pass, like a harness error), so pass_rate reflects "N launched".
    )
    if org_id is not None:
        stmt = stmt.where(TrialModel.org_id == org_id)
    return stmt.group_by(TrialModel.task_id, TrialModel.task_version_id).subquery()


# Aggregate sort tokens -> (metrics column label added to ranked_tasks, descending).
# The column labels must match the ``add_columns`` labels in browse_tasks_core.
_AGGREGATE_SORTS: dict[str, tuple[str, bool]] = {
    "cost_desc": ("cost_usd", True),
    "avg_score_desc": ("avg_reward", True),
    "avg_score_asc": ("avg_reward", False),
    "total_tokens_desc": ("total_tokens", True),
    "total_tokens_asc": ("total_tokens", False),
    "runtime_total_desc": ("runtime_total", True),
    "runtime_total_asc": ("runtime_total", False),
    "runtime_avg_desc": ("runtime_avg", True),
    "runtime_avg_asc": ("runtime_avg", False),
}

# Phase 2.2: aggregate condition keys allowed inside an OR-group. If any group
# uses one, the ``_task_metrics_subquery`` join is added so the group predicate
# can reference its columns (same columns the global aggregate filters use).
_AGGREGATE_GROUP_KEYS = frozenset(
    {
        "avg_score_min",
        "avg_score_max",
        "total_tokens_min",
        "total_tokens_max",
        "total_trials_min",
        "completed_trials_min",
        "failed_trials_min",
        "pass_count_min",
        "partial_count_min",
        "fail_count_min",
        "harness_count_min",
        "runtime_total_min",
        "runtime_total_max",
        "runtime_avg_min",
        "runtime_avg_max",
        "pass_rate_min",
        "pass_rate_max",
    }
)


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
    statuses: Sequence[str] | None = None,
    priorities: Sequence[str] | None = None,
    verdict_statuses: Sequence[str] | None = None,
    has_link: bool | None = None,
    run_analysis: bool | None = None,
    run_probe: bool | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    trial_finished_after: datetime | None = None,
    trial_finished_before: datetime | None = None,
    experiment_ids: Sequence[str] | None = None,
    agents: Sequence[str] | None = None,
    models: Sequence[str] | None = None,
    agent_models: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    environments: Sequence[str] | None = None,
    trial_statuses: Sequence[str] | None = None,
    origins: Sequence[str] | None = None,
    trial_is_probe: bool | None = None,
    harbor_shas: Sequence[str] | None = None,
    harbor_stages: Sequence[str] | None = None,
    analysis_classifications: Sequence[str] | None = None,
    has_error: bool | None = None,
    has_trajectory: bool | None = None,
    min_attempts: int | None = None,
    min_tokens: int | None = None,
    max_tokens: int | None = None,
    min_steps: int | None = None,
    max_steps: int | None = None,
    min_duration_seconds: float | None = None,
    max_duration_seconds: float | None = None,
    min_tool_calls: int | None = None,
    max_tool_calls: int | None = None,
    tool_names: Sequence[str] | None = None,
    tool_count_mins: Mapping[str, int] | None = None,
    trial_metric_match: str = "any",
    reward_min: float | None = None,
    reward_max: float | None = None,
    # --- Phase 1.2-lite aggregate filters / sort (no migration) ---
    avg_score_min: float | None = None,
    avg_score_max: float | None = None,
    total_tokens_min: int | None = None,
    total_tokens_max: int | None = None,
    total_trials_min: int | None = None,
    completed_trials_min: int | None = None,
    failed_trials_min: int | None = None,
    pass_count_min: int | None = None,
    partial_count_min: int | None = None,
    fail_count_min: int | None = None,
    harness_count_min: int | None = None,
    runtime_total_min: float | None = None,
    runtime_total_max: float | None = None,
    runtime_avg_min: float | None = None,
    runtime_avg_max: float | None = None,
    pass_rate_min: float | None = None,
    pass_rate_max: float | None = None,
    sort: str | None = None,
    # --- Phase 2.1 agent/model comparison (no migration) ---
    compare_by: str | None = None,
    compare_a: str | None = None,
    compare_b: str | None = None,
    compare_metric: str | None = None,
    compare_agg: str | None = None,
    compare_margin: float | None = None,
    compare_margin_unit: str | None = None,
    # --- Phase 2.3 top performer (best agent/model per task), no migration ---
    top_by: str | None = None,
    top_value: str | None = None,
    top_metric: str | None = None,
    # --- Phase 2.2 OR-groups ("Match any of…"), no migration ---
    or_groups: Sequence[Mapping[str, Any]] | None = None,
    record_timing: TimingRecorder | None = None,
) -> TaskBrowseResponse:
    """List latest-version task summaries for the task browser.

    Beyond the free-text / tag / author filters, the browser supports a set of
    "Phase 1.1.1" direct filters that require no schema change:

    * Task-column filters (``statuses``, ``priorities``, ``verdict_statuses``,
      ``has_link``, ``run_analysis``, ``run_probe``, ``created_after`` /
      ``created_before``, ``experiment_ids``) are plain ``WHERE`` predicates on
      the ``tasks`` row.
    * Trial-level filters (``agents``, ``models``, ``trial_finished_*``, ``providers``,
      ``environments``, ``trial_statuses``, ``origins``, ``trial_is_probe``,
      ``harbor_*``, ``analysis_classifications``, ``has_error``,
      ``has_trajectory``, ``min_attempts``, token / step / reward ranges) are
      "task has at least one current-version trial matching X" — expressed as a
      correlated ``EXISTS`` over ``trials`` (non-superseded, current version,
      non-probe unless ``trial_is_probe`` is set). They filter the task set; they
      are NOT aggregates, so e.g. a token range matches a single trial's value,
      not the task average.
    * "Phase 1.2-lite" AGGREGATE filters/sort (``avg_score_*``, ``total_tokens_*``,
      ``*_trials_min``, ``pass/partial/fail/harness_count_min``, ``runtime_*``,
      ``sort``) are computed on the fly by ``_task_metrics_subquery`` — a single
      GROUP BY over the same scoped trial set — and applied as HAVING-equivalent
      predicates before pagination. There is NO supporting index or roll-up; this
      is the deliberately-slow path that full Phase 1.2 will denormalize. Cost sort
      uses persisted costs plus the same token estimates shown on task cards.
    """

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

    # --- Phase 1.1.1 direct filters (no schema change) ---------------------
    # Task-column predicates: plain WHERE on the tasks row.
    if statuses:
        ranked_tasks = ranked_tasks.where(TaskModel.status.in_(list(statuses)))
    if priorities:
        ranked_tasks = ranked_tasks.where(TaskModel.priority.in_(list(priorities)))
    if verdict_statuses:
        ranked_tasks = ranked_tasks.where(
            TaskModel.verdict_status.in_(list(verdict_statuses))
        )
    if has_link is not None:
        ranked_tasks = ranked_tasks.where(
            TaskModel.link.isnot(None) if has_link else TaskModel.link.is_(None)
        )
    if run_analysis is not None:
        ranked_tasks = ranked_tasks.where(TaskModel.run_analysis.is_(run_analysis))
    if run_probe is not None:
        ranked_tasks = ranked_tasks.where(TaskModel.run_probe.is_(run_probe))
    if created_after is not None:
        ranked_tasks = ranked_tasks.where(TaskModel.created_at >= created_after)
    if created_before is not None:
        ranked_tasks = ranked_tasks.where(TaskModel.created_at <= created_before)
    if experiment_ids:
        ranked_tasks = ranked_tasks.where(
            exists(
                select(task_experiments.c.task_id).where(
                    task_experiments.c.task_id == TaskModel.id,
                    task_experiments.c.experiment_id.in_(list(experiment_ids)),
                    task_experiments.c.deleted_at.is_(None),
                )
            )
        )

    # Trial-level predicates: "task has >=1 current-version trial matching".
    def _trial_exists(*predicates: Any, include_probes: bool = False) -> Any:
        conds = [
            TrialModel.task_id == TaskModel.id,
            TrialModel.task_version_id == TaskModel.current_version_id,
            TrialModel.superseded_by_trial_id.is_(None),
        ]
        if not include_probes:
            conds.append(TrialModel.is_probe.isnot(True))
        conds.extend(predicates)
        return exists(select(TrialModel.id).where(and_(*conds)))

    if agents:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.agent.in_(list(agents)))
        )
    trial_finished_predicates = []
    metric_filter = TrialMetricFilter.from_query(
        models=models,
        min_steps=min_steps,
        max_steps=max_steps,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        min_tool_calls=min_tool_calls,
        max_tool_calls=max_tool_calls,
        tool_names=tool_names,
        tool_count_mins=tool_count_mins,
        match=trial_metric_match,
    )
    # Models always ride the metric predicate (same eligible-trial contract and
    # deleted_at handling as the dashboard); finished-at bounds join its scope
    # when it's active so every constraint is checked against the same trial.
    metric_filter_active = not metric_filter.is_empty
    if trial_finished_after is not None:
        trial_finished_predicates.append(
            TrialModel.finished_at >= trial_finished_after
        )
    if trial_finished_before is not None:
        trial_finished_predicates.append(
            TrialModel.finished_at <= trial_finished_before
        )
    if trial_finished_predicates and not metric_filter_active:
        ranked_tasks = ranked_tasks.where(_trial_exists(*trial_finished_predicates))
    if agent_models:
        # Each token is "agent:model" (model = everything after the first colon;
        # agent names have no colons) or bare "agent" for a null model. Match a
        # single trial whose (agent, model) equals one of the selected pairs.
        pair_conds = []
        for token in agent_models:
            agent_name, sep, model_name = token.partition(":")
            if not agent_name:
                continue
            if sep and model_name:
                pair_conds.append(
                    and_(
                        TrialModel.agent == agent_name,
                        TrialModel.model == model_name,
                    )
                )
            else:
                pair_conds.append(
                    and_(
                        TrialModel.agent == agent_name,
                        TrialModel.model.is_(None),
                    )
                )
        if pair_conds:
            ranked_tasks = ranked_tasks.where(_trial_exists(or_(*pair_conds)))
    if providers:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.provider.in_(list(providers)))
        )
    if environments:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.environment.in_(list(environments)))
        )
    if trial_statuses:
        # Values are the uppercase enum NAMES (SUCCESS/FAILED/…), matching the
        # frontend TRIAL_STATUS_OPTIONS. TrialModel.status uses SQLEnum without
        # values_callable, so the column stores names, not the lowercase values.
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.status.in_(list(trial_statuses)))
        )
    if origins:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.origin.in_(list(origins)))
        )
    if trial_is_probe is not None:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.is_probe.is_(trial_is_probe), include_probes=True)
        )
    if harbor_shas:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.harbor_sha.in_(list(harbor_shas)))
        )
    if harbor_stages:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.harbor_stage.in_(list(harbor_stages)))
        )
    if analysis_classifications:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(
                TrialModel.analysis["classification"].astext.in_(
                    list(analysis_classifications)
                )
            )
        )
    if has_error is not None:
        has_errored_trial = _trial_exists(TrialModel.error_message.isnot(None))
        if has_error:
            ranked_tasks = ranked_tasks.where(has_errored_trial)
        else:
            # "No" is the complement of "Yes": the task ran at least one real
            # trial and none of them errored (so a task with a mix of errored
            # and clean trials counts as "Yes", not "No").
            ranked_tasks = ranked_tasks.where(and_(_trial_exists(), ~has_errored_trial))
    if has_trajectory is not None:
        has_trajectory_trial = _trial_exists(TrialModel.has_trajectory.is_(True))
        if has_trajectory:
            ranked_tasks = ranked_tasks.where(has_trajectory_trial)
        else:
            # Same complement semantics as has_error: ran a real trial, none
            # of which has a trajectory.
            ranked_tasks = ranked_tasks.where(
                and_(_trial_exists(), ~has_trajectory_trial)
            )
    if min_attempts is not None:
        ranked_tasks = ranked_tasks.where(
            _trial_exists(TrialModel.attempts >= min_attempts)
        )
    if min_tokens is not None or max_tokens is not None:
        # Token size = input + output + cache for a single trial (per-trial
        # existence, not a task aggregate). Backs the "Token size" sidebar filter.
        total_tokens = (
            func.coalesce(TrialModel.input_tokens, 0)
            + func.coalesce(TrialModel.output_tokens, 0)
            + func.coalesce(TrialModel.cache_tokens, 0)
        )
        token_preds = []
        if min_tokens is not None:
            token_preds.append(total_tokens >= min_tokens)
        if max_tokens is not None:
            token_preds.append(total_tokens <= max_tokens)
        ranked_tasks = ranked_tasks.where(_trial_exists(*token_preds))
    if metric_filter_active:
        metric_predicate = build_trial_metric_predicate(
            metric_filter,
            scope=EligibleTrialScope(
                membership=(
                    TrialModel.task_id == TaskModel.id,
                    TrialModel.task_version_id == TaskModel.current_version_id,
                    *trial_finished_predicates,
                )
            ),
        )
        if metric_predicate is not None:
            ranked_tasks = ranked_tasks.where(metric_predicate)
    if reward_min is not None or reward_max is not None:
        reward_preds = [TrialModel.reward.isnot(None)]
        if reward_min is not None:
            reward_preds.append(TrialModel.reward >= reward_min)
        if reward_max is not None:
            reward_preds.append(TrialModel.reward <= reward_max)
        ranked_tasks = ranked_tasks.where(_trial_exists(*reward_preds))

    # --- Phase 1.2-lite aggregate filters / sort (no migration) -----------
    # Unlike the ``_trial_exists`` filters above (which match a SINGLE trial),
    # these are TASK aggregates over the scoped trial set and must be computed
    # in SQL BEFORE pagination. We build one GROUP BY subquery and LEFT JOIN it
    # on the task's current version, then apply the range predicates as WHERE on
    # the aggregated columns (HAVING semantics) so tasks are filtered out of the
    # ranked set before ``name_rank`` / LIMIT. The join + columns are added ONLY
    # when an aggregate filter or aggregate sort is requested, so the default
    # browse query is byte-for-byte unchanged. Each aggregate is an on-the-fly
    # computation to be denormalized in full Phase 1.2 (see module note above).
    aggregate_sort_active = sort in _AGGREGATE_SORTS
    aggregate_filter_active = any(
        value is not None
        for value in (
            avg_score_min,
            avg_score_max,
            total_tokens_min,
            total_tokens_max,
            total_trials_min,
            completed_trials_min,
            failed_trials_min,
            pass_count_min,
            partial_count_min,
            fail_count_min,
            harness_count_min,
            runtime_total_min,
            runtime_total_max,
            runtime_avg_min,
            runtime_avg_max,
            pass_rate_min,
            pass_rate_max,
        )
    )
    # Phase 2.2 OR-groups ("Match any of…"): drop empty / non-mapping specs. If any
    # group uses an aggregate condition, the metrics join must be present so the
    # group predicate can reference its columns.
    group_specs = [
        spec for spec in (or_groups or []) if isinstance(spec, Mapping) and spec
    ]
    groups_use_aggregates = any(
        any(key in _AGGREGATE_GROUP_KEYS for key in spec) for spec in group_specs
    )
    task_metrics = None
    if aggregate_filter_active or aggregate_sort_active or groups_use_aggregates:
        task_metrics = _task_metrics_subquery(
            org_id,
            cost_models=models,
            cost_finished_after=trial_finished_after,
            cost_finished_before=trial_finished_before,
        )
        ranked_tasks = ranked_tasks.add_columns(
            task_metrics.c.avg_reward.label("avg_reward"),
            task_metrics.c.total_tokens.label("total_tokens"),
            task_metrics.c.cost_usd.label("cost_usd"),
            task_metrics.c.total_trials.label("agg_total_trials"),
            task_metrics.c.completed_trials.label("agg_completed_trials"),
            task_metrics.c.failed_trials.label("agg_failed_trials"),
            task_metrics.c.pass_count.label("pass_count"),
            task_metrics.c.partial_count.label("partial_count"),
            task_metrics.c.fail_count.label("fail_count"),
            task_metrics.c.harness_count.label("harness_count"),
            task_metrics.c.pass_rate.label("pass_rate"),
            task_metrics.c.runtime_total.label("runtime_total"),
            task_metrics.c.runtime_avg.label("runtime_avg"),
        ).outerjoin(
            task_metrics,
            and_(
                task_metrics.c.task_id == TaskModel.id,
                task_metrics.c.task_version_id == TaskModel.current_version_id,
            ),
        )
        # ``avg_score`` is a PERCENT (0-100) at this boundary; ``reward`` is 0-1.
        # A task with no scored trials has a NULL avg and drops out of both the
        # min and the max bound (NULL comparisons are not-true) — undefined score
        # is neither "high" nor "low".
        if avg_score_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.avg_reward * 100 >= avg_score_min
            )
        if avg_score_max is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.avg_reward * 100 <= avg_score_max
            )
        if total_tokens_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.total_tokens >= total_tokens_min
            )
        if total_tokens_max is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.total_tokens <= total_tokens_max
            )
        if total_trials_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.total_trials >= total_trials_min
            )
        if completed_trials_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.completed_trials >= completed_trials_min
            )
        if failed_trials_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.failed_trials >= failed_trials_min
            )
        if pass_count_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.pass_count >= pass_count_min
            )
        if partial_count_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.partial_count >= partial_count_min
            )
        if fail_count_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.fail_count >= fail_count_min
            )
        if harness_count_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.harness_count >= harness_count_min
            )
        if runtime_total_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.runtime_total >= runtime_total_min
            )
        if runtime_total_max is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.runtime_total <= runtime_total_max
            )
        if runtime_avg_min is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.runtime_avg >= runtime_avg_min
            )
        if runtime_avg_max is not None:
            ranked_tasks = ranked_tasks.where(
                task_metrics.c.runtime_avg <= runtime_avg_max
            )
        if pass_rate_min is not None:
            ranked_tasks = ranked_tasks.where(task_metrics.c.pass_rate >= pass_rate_min)
        if pass_rate_max is not None:
            ranked_tasks = ranked_tasks.where(task_metrics.c.pass_rate <= pass_rate_max)

    # --- Phase 2.3 top performer (best agent/model per task) --------------
    top_subject_col = _compare_subject_column(top_by) if top_by else None
    if (
        top_subject_col is not None
        and top_value
        and top_metric in _COMPARE_METRIC_HIGHER_BETTER
    ):
        ranked_tasks = ranked_tasks.where(
            _top_performer_predicate(
                org_id,
                subject_col=top_subject_col,
                value=top_value,
                metric=top_metric,
            )
        )

    # --- Phase 2.1 agent/model comparison (no migration) ------------------
    # Keep only tasks where subject A beats subject B on one metric, computed on
    # the fly. Missing-subject tasks drop out automatically: a task lacking A or B
    # yields a NULL a_val/b_val, so the comparison is NULL -> not-true -> excluded
    # (this is the "exclude if a task has only one of the two" behaviour). Only
    # built when a valid, distinct pair + a known metric are requested.
    compare_subject_col = _compare_subject_column(compare_by) if compare_by else None
    compare_metric_known = (
        compare_metric in _COMPARE_METRIC_HIGHER_BETTER if compare_metric else False
    )
    if (
        compare_subject_col is not None
        and compare_metric_known
        and compare_a
        and compare_b
        and compare_a != compare_b
    ):
        higher_better = _COMPARE_METRIC_HIGHER_BETTER[compare_metric]
        agg = compare_agg if compare_agg in ("avg", "median") else "best"
        compare_metrics = _agent_compare_subquery(
            org_id,
            subject_col=compare_subject_col,
            value_a=compare_a,
            value_b=compare_b,
            metric=compare_metric,
            agg=agg,
        )
        ranked_tasks = ranked_tasks.outerjoin(
            compare_metrics,
            and_(
                compare_metrics.c.task_id == TaskModel.id,
                compare_metrics.c.task_version_id == TaskModel.current_version_id,
            ),
        )
        a_val = compare_metrics.c.a_val
        b_val = compare_metrics.c.b_val
        # Optional margin: A must beat B by MORE than ``compare_margin`` — a
        # percent of B (``pct``, default) or an absolute difference (``abs``).
        if compare_margin is not None and compare_margin > 0:
            if compare_margin_unit == "abs":
                threshold = (
                    b_val + compare_margin if higher_better else b_val - compare_margin
                )
            else:
                factor = (
                    1 + compare_margin / 100.0
                    if higher_better
                    else 1 - compare_margin / 100.0
                )
                threshold = b_val * factor
        else:
            threshold = b_val
        ranked_tasks = ranked_tasks.where(
            a_val > threshold if higher_better else a_val < threshold
        )

    # --- Phase 2.2 OR-groups ("Match any of…"), no migration --------------
    # DNF combinator scoped to one block: each group's conditions are ANDed, the
    # groups are ORed, and the whole thing is intersected (ANDed) with the global
    # filters above via a SINGLE extra ``.where(or_(...))``. Conditions reuse the
    # same predicate primitives as the global filters (``_trial_exists`` for the
    # trial-level ones; ``task_metrics`` columns for the aggregate ones), so a
    # group is "a mini-sidebar". Compare A vs B is global-only in v1.
    def _group_predicate(spec: Mapping[str, Any]) -> Any:
        preds: list[Any] = []

        def _list(key: str) -> list[Any] | None:
            value = spec.get(key)
            return list(value) if value else None

        if (statuses_ := _list("statuses")) is not None:
            preds.append(TaskModel.status.in_(statuses_))
        if (priorities_ := _list("priorities")) is not None:
            preds.append(TaskModel.priority.in_(priorities_))
        if (verdicts_ := _list("verdict_statuses")) is not None:
            preds.append(TaskModel.verdict_status.in_(verdicts_))
        if spec.get("has_link") is not None:
            preds.append(
                TaskModel.link.isnot(None)
                if spec["has_link"]
                else TaskModel.link.is_(None)
            )
        if spec.get("run_analysis") is not None:
            preds.append(TaskModel.run_analysis.is_(bool(spec["run_analysis"])))
        if spec.get("run_probe") is not None:
            preds.append(TaskModel.run_probe.is_(bool(spec["run_probe"])))

        if (agents_ := _list("agents")) is not None:
            preds.append(_trial_exists(TrialModel.agent.in_(agents_)))
        if (models_ := _list("models")) is not None:
            preds.append(_trial_exists(TrialModel.model.in_(models_)))
        if (agent_models_ := _list("agent_models")) is not None:
            pair_conds = []
            for token in agent_models_:
                agent_name, sep, model_name = str(token).partition(":")
                if not agent_name:
                    continue
                if sep and model_name:
                    pair_conds.append(
                        and_(
                            TrialModel.agent == agent_name,
                            TrialModel.model == model_name,
                        )
                    )
                else:
                    pair_conds.append(
                        and_(
                            TrialModel.agent == agent_name,
                            TrialModel.model.is_(None),
                        )
                    )
            if pair_conds:
                preds.append(_trial_exists(or_(*pair_conds)))
        if (providers_ := _list("providers")) is not None:
            preds.append(_trial_exists(TrialModel.provider.in_(providers_)))
        if (environments_ := _list("environments")) is not None:
            preds.append(_trial_exists(TrialModel.environment.in_(environments_)))
        if (trial_statuses_ := _list("trial_statuses")) is not None:
            preds.append(_trial_exists(TrialModel.status.in_(trial_statuses_)))
        if (origins_ := _list("origins")) is not None:
            preds.append(_trial_exists(TrialModel.origin.in_(origins_)))
        if (classifications_ := _list("analysis_classifications")) is not None:
            preds.append(
                _trial_exists(
                    TrialModel.analysis["classification"].astext.in_(classifications_)
                )
            )
        if spec.get("trial_is_probe") is not None:
            preds.append(
                _trial_exists(
                    TrialModel.is_probe.is_(bool(spec["trial_is_probe"])),
                    include_probes=True,
                )
            )
        if spec.get("has_error") is not None:
            errored = _trial_exists(TrialModel.error_message.isnot(None))
            preds.append(
                errored if spec["has_error"] else and_(_trial_exists(), ~errored)
            )
        if spec.get("has_trajectory") is not None:
            traj = _trial_exists(TrialModel.has_trajectory.is_(True))
            preds.append(
                traj if spec["has_trajectory"] else and_(_trial_exists(), ~traj)
            )
        if spec.get("min_attempts") is not None:
            preds.append(_trial_exists(TrialModel.attempts >= spec["min_attempts"]))

        min_tok, max_tok = spec.get("min_tokens"), spec.get("max_tokens")
        if min_tok is not None or max_tok is not None:
            tokens_expr = _trial_total_tokens()
            tok_preds = []
            if min_tok is not None:
                tok_preds.append(tokens_expr >= min_tok)
            if max_tok is not None:
                tok_preds.append(tokens_expr <= max_tok)
            preds.append(_trial_exists(*tok_preds))
        min_st, max_st = spec.get("min_steps"), spec.get("max_steps")
        if min_st is not None or max_st is not None:
            step_preds = [TrialModel.total_steps.isnot(None)]
            if min_st is not None:
                step_preds.append(TrialModel.total_steps >= min_st)
            if max_st is not None:
                step_preds.append(TrialModel.total_steps <= max_st)
            preds.append(_trial_exists(*step_preds))
        r_min, r_max = spec.get("reward_min"), spec.get("reward_max")
        if r_min is not None or r_max is not None:
            r_preds = [TrialModel.reward.isnot(None)]
            if r_min is not None:
                r_preds.append(TrialModel.reward >= r_min)
            if r_max is not None:
                r_preds.append(TrialModel.reward <= r_max)
            preds.append(_trial_exists(*r_preds))

        # Aggregate conditions reference the metrics join (present because
        # ``groups_use_aggregates`` forced it above).
        if task_metrics is not None:
            m = task_metrics.c
            agg_preds = {
                "avg_score_min": lambda v: m.avg_reward * 100 >= v,
                "avg_score_max": lambda v: m.avg_reward * 100 <= v,
                "total_tokens_min": lambda v: m.total_tokens >= v,
                "total_tokens_max": lambda v: m.total_tokens <= v,
                "total_trials_min": lambda v: m.total_trials >= v,
                "completed_trials_min": lambda v: m.completed_trials >= v,
                "failed_trials_min": lambda v: m.failed_trials >= v,
                "pass_count_min": lambda v: m.pass_count >= v,
                "partial_count_min": lambda v: m.partial_count >= v,
                "fail_count_min": lambda v: m.fail_count >= v,
                "harness_count_min": lambda v: m.harness_count >= v,
                "runtime_total_min": lambda v: m.runtime_total >= v,
                "runtime_total_max": lambda v: m.runtime_total <= v,
                "runtime_avg_min": lambda v: m.runtime_avg >= v,
                "runtime_avg_max": lambda v: m.runtime_avg <= v,
                "pass_rate_min": lambda v: m.pass_rate >= v,
                "pass_rate_max": lambda v: m.pass_rate <= v,
            }
            for key, make_pred in agg_preds.items():
                if spec.get(key) is not None:
                    preds.append(make_pred(spec[key]))

        # Phase 2.3: a "Compare A vs B" condition inside the group, built from
        # correlated subqueries so it composes here (unlike the global joined
        # form). ``spec["compare"]`` mirrors the global compare params.
        cmp = spec.get("compare")
        if isinstance(cmp, Mapping):
            cmp_subject = _compare_subject_column(cmp.get("compare_by") or "agent")
            cmp_metric = cmp.get("compare_metric")
            cmp_a, cmp_b = cmp.get("compare_a"), cmp.get("compare_b")
            if (
                cmp_subject is not None
                and cmp_metric in _COMPARE_METRIC_HIGHER_BETTER
                and cmp_a
                and cmp_b
                and cmp_a != cmp_b
            ):
                cmp_agg = cmp.get("compare_agg")
                preds.append(
                    _compare_predicate_correlated(
                        org_id,
                        subject_col=cmp_subject,
                        value_a=cmp_a,
                        value_b=cmp_b,
                        metric=cmp_metric,
                        agg=cmp_agg if cmp_agg in ("avg", "median") else "best",
                        margin=cmp.get("compare_margin"),
                        margin_unit=cmp.get("compare_margin_unit"),
                    )
                )

        return and_(*preds) if preds else None

    if group_specs:
        group_predicates = [
            pred
            for pred in (_group_predicate(spec) for spec in group_specs)
            if pred is not None
        ]
        if group_predicates:
            ranked_tasks = ranked_tasks.where(or_(*group_predicates))

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
        # A combine copy's created_at is its materialization time, so counting it
        # here would freshen last_run_at and jump an old task up the page even
        # though the card's counters/cost/icons exclude it. The source trial
        # still supplies the real activity time.
        not_combine_copy_filter(),
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
    )

    # Aggregate sort (Phase 1.2-lite): when requested, order by the aggregate
    # column FIRST (nulls last), keeping the existing recency order as the
    # tiebreak so tasks with an equal / NULL metric still fall back to the
    # familiar ordering. The metric columns exist on the subquery only when the
    # aggregate join was added above, hence the ``aggregate_sort_active`` guard.
    aggregate_order = []
    if aggregate_sort_active:
        column_label, descending = _AGGREGATE_SORTS[sort]  # type: ignore[index]
        metric_column = ranked_tasks_subquery.c[column_label]
        aggregate_order.append(
            nulls_last(metric_column.desc() if descending else metric_column.asc())
        )

    paged_rows = (
        paged_rows.order_by(
            *aggregate_order,
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
    cost_by_task: dict[str, dict[str, Any]] = {}
    cost_scope_active = sort == "cost_desc"
    task_version_pairs = [
        (str(row["task_id"]), str(row["current_version_id"]))
        for row in visible_rows
        if row["current_version_id"] is not None
    ]
    task_ids = [str(row["task_id"]) for row in visible_rows]

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
                # Combine copies double-count an execution already counted under
                # its source experiment; drop them so the card's trial counts and
                # cost match the task-detail view (which excludes them too).
                not_combine_copy_filter(),
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
        # All-time experiment membership via ``task_experiments``, matching the
        # task-detail page (and user tags) rather than the current-version trials.
        exp_where = [
            task_experiments.c.task_id.in_(task_ids),
            task_experiments.c.deleted_at.is_(None),
        ]
        if org_id is not None:
            exp_where.append(ExperimentModel.org_id == org_id)
        exp_query = (
            select(
                task_experiments.c.task_id.label("task_id"),
                ExperimentModel.id.label("experiment_id"),
                ExperimentModel.name.label("experiment_name"),
            )
            .select_from(task_experiments)
            .join(
                ExperimentModel,
                ExperimentModel.id == task_experiments.c.experiment_id,
            )
            .where(*exp_where)
            .distinct()
            .order_by(
                task_experiments.c.task_id.asc(),
                ExperimentModel.name.asc(),
                ExperimentModel.id.asc(),
            )
        )
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
                TrialModel.cache_write_tokens.label("cache_write_tokens"),
                TrialModel.billed_user_id.label("billed_user_id"),
                TrialModel.finished_at.label("finished_at"),
            )
            .where(
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.is_probe.isnot(True),
                # Combine copies re-materialize the same execution under other
                # experiments; keeping them rendered as duplicate result icons on
                # the card. Exclude so each execution shows once.
                not_combine_copy_filter(),
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
                    agent=str(trial_row["agent"]),
                    model=trial_row["model"],
                )
            )
            if cost_scope_active:
                if models and trial_row["model"] not in models:
                    continue
                finished_at = trial_row["finished_at"]
                if trial_finished_after is not None and (
                    finished_at is None or finished_at < trial_finished_after
                ):
                    continue
                if trial_finished_before is not None and (
                    finished_at is None or finished_at > trial_finished_before
                ):
                    continue
            resolved_cost, cost_estimated = _resolve_browse_trial_cost(trial_row)
            if resolved_cost is not None:
                cost_agg: dict[str, Any] = cost_by_task.setdefault(
                    task_key,
                    {
                        "cost_usd": 0.0,
                        "cost_trial_count": 0,
                        "cost_has_estimated": False,
                        "cost_has_native": False,
                        "billed_cost_usd": 0.0,
                        "billed_trial_count": 0,
                        "billed_has_estimated": False,
                        "billed_has_native": False,
                    },
                )
                cost_agg["cost_usd"] += resolved_cost
                cost_agg["cost_trial_count"] += 1
                if cost_estimated:
                    cost_agg["cost_has_estimated"] = True
                else:
                    cost_agg["cost_has_native"] = True
                if trial_row["billed_user_id"] is not None:
                    cost_agg["billed_cost_usd"] += resolved_cost
                    cost_agg["billed_trial_count"] += 1
                    if cost_estimated:
                        cost_agg["billed_has_estimated"] = True
                    else:
                        cost_agg["billed_has_native"] = True

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

    # QA spend is a separate ledger with its own grain (one row per analysis
    # job, not per trial), so it is a second aggregate rather than another
    # branch of the loop above. Scoped to the same current-version, non-probe,
    # non-superseded, non-combine-copy trial population the card prices for
    # agent cost, so the card's two figures describe the same trials.
    qa_by_task = await get_task_qa_costs(
        session,
        task_ids=task_ids,
        org_id=org_id,
        trial_scope_pairs=task_version_pairs,
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
                billed_cost_usd=float(
                    cost_by_task.get(str(row["task_id"]), {}).get("billed_cost_usd")
                    or 0.0
                ),
                billed_trial_count=int(
                    cost_by_task.get(str(row["task_id"]), {}).get("billed_trial_count")
                    or 0
                ),
                billed_has_estimated=bool(
                    cost_by_task.get(str(row["task_id"]), {}).get(
                        "billed_has_estimated"
                    )
                ),
                billed_has_native=bool(
                    cost_by_task.get(str(row["task_id"]), {}).get("billed_has_native")
                ),
                qa_cost_usd=(
                    qa_by_task[str(row["task_id"])].qa_cost_usd
                    if str(row["task_id"]) in qa_by_task
                    else 0.0
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


async def browse_task_facets_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
) -> TaskBrowseFacets:
    """Distinct filter-option values for the task browser.

    Returns the trial-derived facets (agent, model, provider, environment,
    harbor stage, analysis classification), scoped to the same trials the
    browse filters match — non-probe, non-superseded, and on each task's
    CURRENT version — so every option yields at least one task.

    ``experiments`` is deprecated and always empty. It used to serialize every
    org experiment (measured 7.7MB / 126k entries on one org), so experiment
    filter options now come from the scoped, searchable
    ``browse_experiment_options_core`` (GET /tasks/browse/experiment-options).
    """

    def _distinct(column: Any) -> Any:
        stmt = (
            select(column)
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(
                column.isnot(None),
                TrialModel.is_probe.isnot(True),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.task_version_id == TaskModel.current_version_id,
            )
        )
        if org_id is not None:
            stmt = stmt.where(TrialModel.org_id == org_id)
        return stmt.distinct().order_by(column)

    # Each facet is computed independently and a failure in one (e.g. a
    # data-dependent JSON query) must not 500 the whole endpoint and blank every
    # filter dropdown. On error we log, roll back the aborted transaction so the
    # next query runs clean, and return an empty list for just that facet.
    async def _values(column: Any, name: str) -> list[str]:
        try:
            result = await session.execute(_distinct(column))
            return [v for v in result.scalars().all() if v]
        except Exception:  # noqa: BLE001 - facets are best-effort
            logger.exception("browse facets: %s query failed", name)
            await session.rollback()
            return []

    classification = TrialModel.analysis["classification"].astext

    agents = await _values(TrialModel.agent, "agent")
    models = await _values(TrialModel.model, "model")
    providers = await _values(TrialModel.provider, "provider")

    # Distinct (agent, model) pairs — the meaningful "what was run" unit. A trial
    # is an agent at a specific model, so this is the primary run-config facet.
    agent_models: list[AgentModelFacet] = []
    try:
        pairs_stmt = (
            select(TrialModel.agent, TrialModel.model)
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(
                TrialModel.agent.isnot(None),
                TrialModel.is_probe.isnot(True),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.task_version_id == TaskModel.current_version_id,
            )
        )
        if org_id is not None:
            pairs_stmt = pairs_stmt.where(TrialModel.org_id == org_id)
        pairs_stmt = pairs_stmt.distinct().order_by(TrialModel.agent, TrialModel.model)
        pair_rows = (await session.execute(pairs_stmt)).all()
        agent_models = [
            AgentModelFacet(agent=row[0], model=row[1]) for row in pair_rows
        ]
    except Exception:  # noqa: BLE001 - facets are best-effort
        logger.exception("browse facets: agent_models query failed")
        await session.rollback()
    environments = await _values(TrialModel.environment, "environment")
    harbor_stages = await _values(TrialModel.harbor_stage, "harbor_stage")
    analysis_classifications = await _values(classification, "analysis_classification")

    return TaskBrowseFacets(
        agents=agents,
        models=models,
        agent_models=agent_models,
        providers=providers,
        environments=environments,
        harbor_stages=harbor_stages,
        analysis_classifications=analysis_classifications,
        # Deprecated, always empty — see browse_experiment_options_core.
        experiments=[],
    )


# Hard ceiling on experiment options returned (and on ids hydrated) per call.
EXPERIMENT_OPTIONS_MAX_LIMIT = 200


async def browse_experiment_options_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    query: str | None = None,
    ids: Sequence[str] | None = None,
    limit: int = 50,
) -> ExperimentOptionsResponse:
    """Option source for the experiment browse filter, with two access modes.

    Hydration (``ids`` given): a keyed lookup of specific experiments —
    filter chips restored from a URL or saved filter. The result set is
    defined by the input ids (deduped, capped at
    ``EXPERIMENT_OPTIONS_MAX_LIMIT``); ``limit`` and ``query`` are search
    concepts and do not apply.

    Search (no ``ids``): at most ``limit`` (capped at
    ``EXPERIMENT_OPTIONS_MAX_LIMIT``) live experiments ordered by name,
    optionally narrowed by a case-insensitive ``query`` substring. The
    population matches the retired ``facets.experiments`` exactly: every live
    org experiment, whether or not it currently has browse-visible tasks.
    """
    base = select(ExperimentModel.id, ExperimentModel.name)
    if org_id is not None:
        base = base.where(ExperimentModel.org_id == org_id)

    if ids:
        # Keyed lookup: the (deduped, capped) id list is the bound — never a
        # page size, so a restored selection always hydrates every chip.
        wanted = list(dict.fromkeys(ids))[:EXPERIMENT_OPTIONS_MAX_LIMIT]
        stmt = base.where(ExperimentModel.id.in_(wanted)).order_by(
            ExperimentModel.name, ExperimentModel.id
        )
        rows = (await session.execute(stmt)).all()
        return ExperimentOptionsResponse(
            items=[ExperimentOption(id=row[0], name=row[1]) for row in rows]
        )

    limit = max(1, min(limit, EXPERIMENT_OPTIONS_MAX_LIMIT))
    if query and query.strip():
        escaped = (
            query.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        base = base.where(ExperimentModel.name.ilike(f"%{escaped}%", escape="\\"))
    stmt = base.order_by(ExperimentModel.name, ExperimentModel.id).limit(limit)
    rows = (await session.execute(stmt)).all()
    return ExperimentOptionsResponse(
        items=[ExperimentOption(id=row[0], name=row[1]) for row in rows]
    )


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
