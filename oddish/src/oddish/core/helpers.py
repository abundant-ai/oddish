from __future__ import annotations

import heapq
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import (
    ExperimentModel,
    Priority,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.model_pricing import estimate_cost_usd
from oddish.schemas import (
    TaskStatusResponse,
    TrialQueueInfo,
    TrialResponse,
    VisibleWorkerJob,
)


def _resolve_trial_cost(
    trial: TrialModel, model_name: str | None
) -> tuple[float | None, bool | None]:
    """Return ``(cost_usd, cost_is_estimated)`` for a trial.

    Prefers the native cost reported by the agent runtime. Falls back to
    estimating from the pricing table when native cost is missing but we
    have token counts and a known model.
    """
    if trial.cost_usd is not None:
        return float(trial.cost_usd), False
    if trial.input_tokens is None and trial.output_tokens is None:
        return None, None
    estimated = estimate_cost_usd(
        model_name or trial.model,
        trial.input_tokens,
        trial.output_tokens,
        trial.cache_tokens,
    )
    if estimated is None:
        return None, None
    return estimated, True


_ANALYSIS_SUMMARY_UNSET = object()
_VERSION_ID_UNSET: object = object()
_QUEUE_PENDING_STATUSES = {TrialStatus.QUEUED, TrialStatus.RETRYING}
_QUEUE_ACTIVE_STATUSES = _QUEUE_PENDING_STATUSES | {TrialStatus.RUNNING}
_VISIBLE_ACTIVE_WORKER_JOB_STATUSES = {
    WorkerJobStatus.QUEUED,
    WorkerJobStatus.RUNNING,
    WorkerJobStatus.RETRYING,
    WorkerJobStatus.BLOCKED,
}


@dataclass(frozen=True)
class _QueueSnapshotTrial:
    trial_id: str
    queue_key: str
    status: TrialStatus
    created_at: datetime
    priority: Priority
    fairness_key: str


def _build_trial_queue_info_snapshot(
    active_trials: Sequence[_QueueSnapshotTrial],
    *,
    target_trial_ids: set[str],
) -> dict[str, TrialQueueInfo]:
    """Simulate claim order for the current queue snapshot."""
    trials_by_queue: dict[str, list[_QueueSnapshotTrial]] = defaultdict(list)
    for trial in active_trials:
        trials_by_queue[trial.queue_key].append(trial)

    queue_info_by_trial_id: dict[str, TrialQueueInfo] = {}

    for queue_key, queue_trials in trials_by_queue.items():
        running_by_fairness: dict[str, int] = defaultdict(int)
        queued_by_priority: dict[Priority, dict[str, list[_QueueSnapshotTrial]]] = {
            Priority.HIGH: defaultdict(list),
            Priority.LOW: defaultdict(list),
        }
        queued_count = 0
        running_count = 0

        for trial in queue_trials:
            if trial.status == TrialStatus.RUNNING:
                running_count += 1
                running_by_fairness[trial.fairness_key] += 1
                continue

            if trial.status in _QUEUE_PENDING_STATUSES:
                queued_count += 1
                queued_by_priority[trial.priority][trial.fairness_key].append(trial)

        for fairness_groups in queued_by_priority.values():
            for queued_trials in fairness_groups.values():
                queued_trials.sort(key=lambda trial: (trial.created_at, trial.trial_id))

        position = 1
        concurrency_limit = settings.get_model_concurrency(queue_key)

        for priority in (Priority.HIGH, Priority.LOW):
            fairness_groups = queued_by_priority[priority]
            heap: list[tuple[int, datetime, str, str, int]] = []

            for fairness_key, queued_trials in fairness_groups.items():
                first_trial = queued_trials[0]
                heap.append(
                    (
                        running_by_fairness.get(fairness_key, 0),
                        first_trial.created_at,
                        first_trial.trial_id,
                        fairness_key,
                        0,
                    )
                )

            heapq.heapify(heap)

            while heap:
                current_running_count, _, trial_id, fairness_key, trial_index = (
                    heapq.heappop(heap)
                )

                if trial_id in target_trial_ids:
                    queue_info_by_trial_id[trial_id] = TrialQueueInfo(
                        position=position,
                        ahead=position - 1,
                        queued_count=queued_count,
                        running_count=running_count,
                        concurrency_limit=concurrency_limit,
                    )

                position += 1
                next_running_count = current_running_count + 1
                running_by_fairness[fairness_key] = next_running_count

                next_trial_index = trial_index + 1
                queued_trials = fairness_groups[fairness_key]
                if next_trial_index >= len(queued_trials):
                    continue

                next_trial = queued_trials[next_trial_index]
                heapq.heappush(
                    heap,
                    (
                        next_running_count,
                        next_trial.created_at,
                        next_trial.trial_id,
                        fairness_key,
                        next_trial_index,
                    ),
                )

    return queue_info_by_trial_id


async def fetch_trial_queue_info(
    session: AsyncSession, *, trials: Sequence[TrialModel]
) -> dict[str, TrialQueueInfo]:
    """Return live queue snapshots for queued/retrying trials."""
    queued_trials = [
        trial for trial in trials if trial.status in _QUEUE_PENDING_STATUSES
    ]
    if not queued_trials:
        return {}

    target_trial_ids = {trial.id for trial in queued_trials}
    queue_keys = sorted({trial.queue_key for trial in queued_trials if trial.queue_key})
    if not queue_keys:
        return {}

    result = await session.execute(
        select(
            TrialModel.id,
            TrialModel.queue_key,
            TrialModel.status,
            TrialModel.created_at,
            TaskModel.priority,
            func.coalesce(TaskModel.created_by_user_id, TaskModel.user).label(
                "fairness_key"
            ),
        )
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(
            TrialModel.queue_key.in_(queue_keys),
            TrialModel.status.in_(tuple(_QUEUE_ACTIVE_STATUSES)),
        )
    )

    active_trials = [
        _QueueSnapshotTrial(
            trial_id=row.id,
            queue_key=str(row.queue_key),
            status=row.status,
            created_at=row.created_at,
            priority=row.priority,
            fairness_key=str(row.fairness_key),
        )
        for row in result.all()
    ]

    return _build_trial_queue_info_snapshot(
        active_trials,
        target_trial_ids=target_trial_ids,
    )


def _resolve_trial_version_fields(
    trial: TrialModel,
) -> tuple[int | None, str | None]:
    """Extract version number and id from a trial's linked TaskVersionModel."""
    version_id = trial.task_version_id
    if version_id is None:
        return None, None
    # Parse version number from the id convention "{task_id}-v{N}"
    parts = version_id.rsplit("-v", 1)
    version_number = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
    return version_number, version_id


def _normalize_worker_job_kind(kind: object) -> str:
    value = getattr(kind, "value", kind)
    return str(value).lower()


def _normalize_worker_job_status(status: object) -> str:
    value = getattr(status, "value", status)
    return str(value).lower()


def build_visible_worker_job(job: WorkerJobModel) -> VisibleWorkerJob:
    return VisibleWorkerJob(
        id=job.id,
        kind=_normalize_worker_job_kind(job.kind),
        status=_normalize_worker_job_status(job.status),
        queue_key=settings.normalize_queue_key(job.queue_key),
        subject_table=job.subject_table,
        subject_id=job.subject_id,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        created_at=job.created_at,
        started_at=job.started_at,
        claimed_at=job.claimed_at,
        heartbeat_at=job.heartbeat_at,
        finished_at=job.finished_at,
        error_message=job.error_message,
    )


async def fetch_visible_worker_jobs(
    session: AsyncSession,
    *,
    task_ids: Sequence[str] = (),
    trial_ids: Sequence[str] = (),
    include_recent_terminal: bool = True,
    recent_limit: int = 250,
) -> dict[tuple[str, str], list[VisibleWorkerJob]]:
    """Fetch active/recent worker_jobs keyed by ``(subject_table, subject_id)``."""
    subject_predicates = []
    if task_ids:
        subject_predicates.append(
            (WorkerJobModel.subject_table == "tasks")
            & (WorkerJobModel.subject_id.in_(list(task_ids)))
        )
    if trial_ids:
        subject_predicates.append(
            (WorkerJobModel.subject_table == "trials")
            & (WorkerJobModel.subject_id.in_(list(trial_ids)))
        )
    if not subject_predicates:
        return {}

    status_predicate = WorkerJobModel.status.in_(
        tuple(_VISIBLE_ACTIVE_WORKER_JOB_STATUSES)
    )
    if include_recent_terminal:
        status_predicate = or_(
            status_predicate, WorkerJobModel.finished_at.is_not(None)
        )

    query = (
        select(WorkerJobModel)
        .where(or_(*subject_predicates), status_predicate)
        .order_by(
            case(
                (
                    WorkerJobModel.status.in_(
                        tuple(_VISIBLE_ACTIVE_WORKER_JOB_STATUSES)
                    ),
                    0,
                ),
                else_=1,
            ),
            WorkerJobModel.finished_at.desc().nulls_last(),
            WorkerJobModel.created_at.desc(),
        )
        .limit(recent_limit)
    )

    result = await session.execute(query)
    jobs_by_subject: dict[tuple[str, str], list[VisibleWorkerJob]] = defaultdict(list)
    for job in result.scalars().all():
        if not job.subject_table or not job.subject_id:
            continue
        jobs_by_subject[(job.subject_table, job.subject_id)].append(
            build_visible_worker_job(job)
        )
    return jobs_by_subject


def build_trial_response(
    trial: TrialModel,
    task_path: str,
    *,
    queue_info: TrialQueueInfo | None = None,
    jobs: Sequence[VisibleWorkerJob] | None = None,
) -> TrialResponse:
    """Build a TrialResponse from a TrialModel."""
    normalized_model = settings.normalize_trial_model(trial.agent, trial.model)
    task_version, task_version_id = _resolve_trial_version_fields(trial)
    cost_usd, cost_is_estimated = _resolve_trial_cost(trial, normalized_model)
    return TrialResponse(
        id=trial.id,
        name=trial.name,
        task_id=trial.task_id,
        task_path=task_path,
        task_version=task_version,
        task_version_id=task_version_id,
        experiment_id=trial.experiment_id,
        agent=trial.agent,
        provider=trial.provider,
        queue_key=settings.normalize_queue_key(trial.queue_key),
        model=normalized_model,
        status=trial.status,
        origin=trial.origin,
        attempts=trial.attempts,
        max_attempts=trial.max_attempts,
        harbor_stage=trial.harbor_stage,
        reward=trial.reward,
        error_message=trial.error_message,
        result=trial.result,
        input_tokens=trial.input_tokens,
        cache_tokens=trial.cache_tokens,
        output_tokens=trial.output_tokens,
        cost_usd=cost_usd,
        cost_is_estimated=cost_is_estimated,
        phase_timing=trial.phase_timing,
        has_trajectory=trial.has_trajectory,
        analysis_status=trial.analysis_status,
        analysis=trial.analysis,
        analysis_error=trial.analysis_error,
        jobs=list(jobs or []),
        queue_info=queue_info,
        created_at=trial.created_at,
        started_at=trial.started_at,
        finished_at=trial.finished_at,
    )


def build_compact_trial_response(
    trial: TrialModel,
    task_path: str,
    *,
    analysis_summary: dict[str, str | None] | None | object = _ANALYSIS_SUMMARY_UNSET,
    queue_info: TrialQueueInfo | None = None,
    jobs: Sequence[VisibleWorkerJob] | None = None,
) -> TrialResponse:
    """Build a compact TrialResponse for table views.

    Intentionally omits large payload fields that are not needed by list UIs.
    """
    resolved_analysis_summary: dict[str, str | None] | None = None
    if analysis_summary is _ANALYSIS_SUMMARY_UNSET:
        if isinstance(trial.analysis, dict):
            resolved_analysis_summary = {
                "classification": trial.analysis.get("classification"),
                "subtype": trial.analysis.get("subtype"),
                "evidence": trial.analysis.get("evidence"),
            }
    else:
        resolved_analysis_summary = (
            analysis_summary if isinstance(analysis_summary, dict) else None
        )
    normalized_model = settings.normalize_trial_model(trial.agent, trial.model)
    task_version, task_version_id = _resolve_trial_version_fields(trial)
    cost_usd, cost_is_estimated = _resolve_trial_cost(trial, normalized_model)

    return TrialResponse(
        id=trial.id,
        name=trial.name,
        task_id=trial.task_id,
        task_path=task_path,
        task_version=task_version,
        task_version_id=task_version_id,
        experiment_id=trial.experiment_id,
        agent=trial.agent,
        provider=trial.provider,
        queue_key=settings.normalize_queue_key(trial.queue_key),
        model=normalized_model,
        status=trial.status,
        origin=trial.origin,
        attempts=trial.attempts,
        max_attempts=trial.max_attempts,
        harbor_stage=trial.harbor_stage,
        reward=trial.reward,
        error_message=trial.error_message,
        result=None,
        input_tokens=trial.input_tokens,
        cache_tokens=trial.cache_tokens,
        output_tokens=trial.output_tokens,
        cost_usd=cost_usd,
        cost_is_estimated=cost_is_estimated,
        phase_timing=trial.phase_timing,
        has_trajectory=trial.has_trajectory,
        analysis_status=trial.analysis_status,
        analysis=resolved_analysis_summary,
        analysis_error=None,
        jobs=list(jobs or []),
        queue_info=queue_info,
        created_at=trial.created_at,
        started_at=trial.started_at,
        finished_at=trial.finished_at,
    )


def resolve_task_status(
    task: TaskModel, *, total: int, completed: int, failed: int
) -> TaskStatus:
    """Determine effective task status based on trial counts."""
    if total > 0 and completed + failed >= total:
        return TaskStatus.COMPLETED
    return task.status


def _format_reward_fields(
    *,
    reward_success: int,
    reward_sum: float,
    reward_total: int,
    include_empty_rewards: bool,
) -> tuple[int | None, float | None, int | None]:
    if include_empty_rewards or reward_total > 0:
        return reward_success, reward_sum, reward_total
    return None, None, None


def _parse_github_meta(tags: dict | None) -> dict[str, str] | None:
    if not tags:
        return None
    raw = tags.get("github_meta")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(k): str(v) for k, v in parsed.items()}


def _parse_version_number(version_id: str) -> int:
    """Parse the numeric suffix from a ``{task_id}-v{N}`` version id."""
    parts = version_id.rsplit("-v", 1)
    return int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0


def _resolve_task_version_fields(
    task: TaskModel,
    *,
    effective_version_id: str | None | object = _VERSION_ID_UNSET,
) -> tuple[int | None, str | None]:
    """Extract the version number and id to report for a task.

    Defaults to ``task.current_version_id`` (the global latest).  Pass
    ``effective_version_id`` to report a context-specific version instead —
    for example, the latest version that has trials in the experiment the
    caller is viewing.  Passing ``None`` explicitly clears the version.
    """
    version_id: str | None
    if effective_version_id is _VERSION_ID_UNSET:
        version_id = task.current_version_id
    else:
        version_id = effective_version_id  # type: ignore[assignment]
    if version_id is None:
        return None, None
    parsed = _parse_version_number(version_id)
    return (parsed or None), version_id


def resolve_effective_version_id(
    task: TaskModel,
    *,
    experiment_context_id: str | None = None,
) -> str | None:
    """Return the ``task_version_id`` that best represents ``task`` in context.

    Outside an experiment (``experiment_context_id`` is ``None``) this is the
    task's global ``current_version_id``.  Within an experiment we instead
    use the latest version among trials that belong to that experiment
    (folding in legacy ``NULL``-experiment trials attached to the task).
    This lets an experiment page keep showing the trials it actually ran,
    even if the underlying task has since been re-uploaded and bumped to a
    newer version elsewhere.  Falls back to ``task.current_version_id`` when
    no scoped trial has a ``task_version_id``.
    """
    if experiment_context_id is None:
        return task.current_version_id
    candidates: list[str] = []
    for trial in task.trials or []:
        version_id = getattr(trial, "task_version_id", None)
        if not version_id:
            continue
        trial_exp_id = getattr(trial, "experiment_id", None)
        if trial_exp_id == experiment_context_id or trial_exp_id is None:
            candidates.append(version_id)
    if not candidates:
        return task.current_version_id
    return max(candidates, key=_parse_version_number)


async def fetch_experiment_effective_version_ids(
    session: AsyncSession,
    *,
    experiment_id: str,
    task_ids: Sequence[str],
) -> dict[str, str]:
    """SQL-backed version of :func:`resolve_effective_version_id` for many tasks.

    Used by paths that don't eagerly load ``task.trials`` (e.g. the lightweight
    counts-only task list).  Returns a mapping of ``task_id`` → latest
    ``task_version_id`` among trials belonging to ``experiment_id`` (plus legacy
    ``NULL``-experiment trials).  Tasks with no scoped trials are omitted.
    """
    if not task_ids:
        return {}

    result = await session.execute(
        select(TrialModel.task_id, TrialModel.task_version_id).where(
            TrialModel.task_id.in_(task_ids),
            or_(
                TrialModel.experiment_id == experiment_id,
                TrialModel.experiment_id.is_(None),
            ),
            TrialModel.task_version_id.is_not(None),
        )
    )

    best: dict[str, tuple[int, str]] = {}
    for task_id, version_id in result.all():
        if not version_id:
            continue
        parsed = _parse_version_number(str(version_id))
        existing = best.get(str(task_id))
        if existing is None or parsed > existing[0]:
            best[str(task_id)] = (parsed, str(version_id))
    return {tid: v[1] for tid, v in best.items()}


def get_task_status_trials(
    task: TaskModel,
    *,
    version_id: str | None | object = _VERSION_ID_UNSET,
) -> list[TrialModel]:
    """Return only the trials that should appear in task status views.

    Defaults to filtering against ``task.current_version_id``.  Pass
    ``version_id`` (including ``None`` to disable filtering) to pivot on a
    different version — for example an experiment-scoped effective version
    computed by :func:`resolve_effective_version_id`.
    """
    effective: str | None
    if version_id is _VERSION_ID_UNSET:
        effective = task.current_version_id
    else:
        effective = version_id  # type: ignore[assignment]
    if effective is None:
        return list(task.trials)
    return [trial for trial in task.trials if trial.task_version_id == effective]


def _primary_experiment_for_task(
    task: TaskModel, *, preferred_experiment_id: str | None = None
) -> ExperimentModel | None:
    """Pick the experiment that best represents this task for response payloads.

    With the task ↔ experiments many-to-many relationship, a task can belong
    to several experiments at once. Response shapes that still expose a
    single ``experiment_id``/``experiment_name`` need to pick one:

    - If ``preferred_experiment_id`` is in the task's set, use it (lets
      experiment-scoped list endpoints return the experiment the caller
      is actually looking at).
    - Otherwise fall back to the first linked experiment (stable ordering
      comes from SQLAlchemy's relationship load, which in turn respects
      the association table's ``created_at`` insertion order).
    """
    experiments = list(task.experiments or [])
    if not experiments:
        return None
    if preferred_experiment_id is not None:
        for exp in experiments:
            if exp.id == preferred_experiment_id:
                return exp
    return experiments[0]


def _build_task_status_response(
    task: TaskModel,
    *,
    total: int,
    completed: int,
    failed: int,
    reward_success: int,
    reward_sum: float,
    reward_total: int,
    include_empty_rewards: bool,
    trials: list[TrialResponse] | None,
    jobs: Sequence[VisibleWorkerJob] | None = None,
    experiment_context_id: str | None = None,
    effective_version_id: str | None | object = _VERSION_ID_UNSET,
) -> TaskStatusResponse:
    formatted_reward_success, formatted_reward_sum, formatted_reward_total = (
        _format_reward_fields(
            reward_success=reward_success,
            reward_sum=reward_sum,
            reward_total=reward_total,
            include_empty_rewards=include_empty_rewards,
        )
    )
    current_version, current_version_id = _resolve_task_version_fields(
        task, effective_version_id=effective_version_id
    )
    primary_experiment = _primary_experiment_for_task(
        task, preferred_experiment_id=experiment_context_id
    )
    experiment_id = primary_experiment.id if primary_experiment else ""
    experiment_name = primary_experiment.name if primary_experiment else ""
    experiment_is_public = primary_experiment.is_public if primary_experiment else False
    return TaskStatusResponse(
        id=task.id,
        name=task.name,
        status=resolve_task_status(
            task, total=total, completed=completed, failed=failed
        ),
        priority=task.priority,
        user=task.user,
        github_username=task.tags.get("github_username") if task.tags else None,
        github_meta=_parse_github_meta(task.tags) if task.tags else None,
        task_path=task.task_path,
        experiment_id=experiment_id,
        experiment_name=experiment_name,
        experiment_is_public=experiment_is_public,
        current_version=current_version,
        current_version_id=current_version_id,
        total=total,
        completed=completed,
        failed=failed,
        progress=f"{completed}/{total} completed",
        trials=trials,
        reward_success=formatted_reward_success,
        reward_sum=formatted_reward_sum,
        reward_total=formatted_reward_total,
        run_analysis=task.run_analysis,
        verdict_status=task.verdict_status,
        verdict=task.verdict,
        verdict_error=task.verdict_error,
        jobs=list(jobs or []),
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def build_task_status_response(
    task: TaskModel,
    *,
    include_empty_rewards: bool = True,
    queue_info_by_trial_id: dict[str, TrialQueueInfo] | None = None,
    jobs_by_subject: dict[tuple[str, str], list[VisibleWorkerJob]] | None = None,
    experiment_context_id: str | None = None,
    effective_version_id: str | None | object = _VERSION_ID_UNSET,
) -> TaskStatusResponse:
    """Build a TaskStatusResponse from a TaskModel with eagerly loaded trials.

    When called with ``experiment_context_id`` and no explicit
    ``effective_version_id``, the effective version is auto-derived from the
    task's currently-loaded trials (assumed to already be scoped to the
    experiment by the caller).  This keeps experiment pages showing trials at
    whatever version actually ran in that experiment, even if the task has
    since been re-uploaded to a newer version elsewhere.
    """
    if effective_version_id is _VERSION_ID_UNSET:
        effective_version_id = resolve_effective_version_id(
            task, experiment_context_id=experiment_context_id
        )
    task_trials = get_task_status_trials(task, version_id=effective_version_id)
    total = len(task_trials)
    completed = sum(1 for t in task_trials if t.status == TrialStatus.SUCCESS)
    failed = sum(1 for t in task_trials if t.status == TrialStatus.FAILED)
    reward_success = sum(1 for t in task_trials if t.reward == 1)
    reward_sum = sum(t.reward for t in task_trials if t.reward is not None)
    reward_total = sum(1 for t in task_trials if t.reward is not None)
    trials = [
        build_trial_response(
            t,
            task.task_path,
            queue_info=(
                queue_info_by_trial_id.get(t.id)
                if queue_info_by_trial_id is not None
                else None
            ),
            jobs=(
                jobs_by_subject.get(("trials", t.id), [])
                if jobs_by_subject is not None
                else None
            ),
        )
        for t in task_trials
    ]
    task_jobs = []
    if jobs_by_subject is not None:
        task_jobs.extend(jobs_by_subject.get(("tasks", task.id), []))
        for trial in task_trials:
            task_jobs.extend(jobs_by_subject.get(("trials", trial.id), []))

    return _build_task_status_response(
        task,
        total=total,
        completed=completed,
        failed=failed,
        reward_success=reward_success,
        reward_sum=reward_sum,
        reward_total=reward_total,
        include_empty_rewards=include_empty_rewards,
        trials=trials,
        jobs=task_jobs,
        experiment_context_id=experiment_context_id,
        effective_version_id=effective_version_id,
    )


def build_task_status_response_compact(
    task: TaskModel,
    *,
    include_empty_rewards: bool = True,
    analysis_summaries: dict[str, dict[str, str | None]] | None = None,
    queue_info_by_trial_id: dict[str, TrialQueueInfo] | None = None,
    jobs_by_subject: dict[tuple[str, str], list[VisibleWorkerJob]] | None = None,
    experiment_context_id: str | None = None,
    effective_version_id: str | None | object = _VERSION_ID_UNSET,
) -> TaskStatusResponse:
    """Build TaskStatusResponse with compact per-trial payloads.

    See :func:`build_task_status_response` for the version-scoping semantics.
    """
    if effective_version_id is _VERSION_ID_UNSET:
        effective_version_id = resolve_effective_version_id(
            task, experiment_context_id=experiment_context_id
        )
    task_trials = get_task_status_trials(task, version_id=effective_version_id)
    total = len(task_trials)
    completed = sum(1 for t in task_trials if t.status == TrialStatus.SUCCESS)
    failed = sum(1 for t in task_trials if t.status == TrialStatus.FAILED)
    reward_success = sum(1 for t in task_trials if t.reward == 1)
    reward_sum = sum(t.reward for t in task_trials if t.reward is not None)
    reward_total = sum(1 for t in task_trials if t.reward is not None)
    trials = [
        build_compact_trial_response(
            t,
            task.task_path,
            analysis_summary=(
                analysis_summaries.get(t.id, {})
                if analysis_summaries is not None
                else _ANALYSIS_SUMMARY_UNSET
            ),
            queue_info=(
                queue_info_by_trial_id.get(t.id)
                if queue_info_by_trial_id is not None
                else None
            ),
            jobs=(
                jobs_by_subject.get(("trials", t.id), [])
                if jobs_by_subject is not None
                else None
            ),
        )
        for t in task_trials
    ]
    task_jobs = []
    if jobs_by_subject is not None:
        task_jobs.extend(jobs_by_subject.get(("tasks", task.id), []))
        for trial in task_trials:
            task_jobs.extend(jobs_by_subject.get(("trials", trial.id), []))

    return _build_task_status_response(
        task,
        total=total,
        completed=completed,
        failed=failed,
        reward_success=reward_success,
        reward_sum=reward_sum,
        reward_total=reward_total,
        include_empty_rewards=include_empty_rewards,
        trials=trials,
        jobs=task_jobs,
        experiment_context_id=experiment_context_id,
        effective_version_id=effective_version_id,
    )


async def fetch_trial_analysis_summaries(
    session: AsyncSession,
    *,
    task_ids: Sequence[str] = (),
    trial_ids: Sequence[str] | None = None,
) -> dict[str, dict[str, str | None]]:
    """Fetch only compact analysis fields needed by matrix views."""
    if trial_ids is not None and not trial_ids:
        return {}
    if trial_ids is None and not task_ids:
        return {}

    filters = [TrialModel.analysis.isnot(None)]
    if trial_ids is not None:
        filters.append(TrialModel.id.in_(list(trial_ids)))
    else:
        filters.append(TrialModel.task_id.in_(list(task_ids)))

    result = await session.execute(
        select(
            TrialModel.id,
            TrialModel.analysis["classification"].astext.label("classification"),
            TrialModel.analysis["subtype"].astext.label("subtype"),
            TrialModel.analysis["evidence"].astext.label("evidence"),
        ).where(*filters)
    )

    summaries: dict[str, dict[str, str | None]] = {}
    for row in result.all():
        if row.classification is None and row.subtype is None:
            continue
        summaries[row.id] = {
            "classification": row.classification,
            "subtype": row.subtype,
            "evidence": row.evidence,
        }
    return summaries


async def build_task_status_responses_from_counts(
    session: AsyncSession,
    *,
    tasks: Sequence[TaskModel],
    include_empty_rewards: bool = True,
    experiment_context_id: str | None = None,
    effective_version_id_by_task_id: dict[str, str] | None = None,
    jobs_by_subject: dict[tuple[str, str], list[VisibleWorkerJob]] | None = None,
) -> list[TaskStatusResponse]:
    """Build TaskStatusResponse objects with aggregated trial counts.

    When ``effective_version_id_by_task_id`` is provided, the stats query
    and each response's ``current_version`` field are scoped to that version
    per task — useful for experiment-scoped task lists where the displayed
    counts should reflect the version that actually ran in this experiment.
    """
    if not tasks:
        return []

    task_ids = [task.id for task in tasks]
    effective_map = effective_version_id_by_task_id or {}

    stats_filters = [TrialModel.task_id.in_(task_ids)]
    if effective_map:
        # Match (task_id, task_version_id) pairs so we only count trials at
        # each task's effective version.  Tasks without an effective version
        # still match any of their trials.
        version_pair_predicates = [
            (TrialModel.task_id == tid) & (TrialModel.task_version_id == vid)
            for tid, vid in effective_map.items()
        ]
        unscoped_ids = [tid for tid in task_ids if tid not in effective_map]
        version_predicate = or_(*version_pair_predicates)
        if unscoped_ids:
            version_predicate = or_(
                version_predicate, TrialModel.task_id.in_(unscoped_ids)
            )
        stats_filters.append(version_predicate)

    stats_query = (
        select(
            TrialModel.task_id,
            func.count(TrialModel.id).label("total"),
            func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
                "completed"
            ),
            func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
                "failed"
            ),
            func.count(case((TrialModel.reward == 1, 1))).label("reward_success"),
            func.sum(TrialModel.reward).label("reward_sum"),
            func.count(case((TrialModel.reward.isnot(None), 1))).label("reward_total"),
        )
        .where(*stats_filters)
        .group_by(TrialModel.task_id)
    )

    stats_result = await session.execute(stats_query)
    stats_map = {row.task_id: row for row in stats_result.all()}

    def _effective(task: TaskModel) -> str | None | object:
        return effective_map.get(task.id, _VERSION_ID_UNSET)

    return [
        _build_task_status_response(
            task,
            total=int(stats_map[task.id].total) if task.id in stats_map else 0,
            completed=int(stats_map[task.id].completed) if task.id in stats_map else 0,
            failed=int(stats_map[task.id].failed) if task.id in stats_map else 0,
            reward_success=(
                int(stats_map[task.id].reward_success) if task.id in stats_map else 0
            ),
            reward_sum=(
                float(stats_map[task.id].reward_sum or 0.0)
                if task.id in stats_map
                else 0.0
            ),
            reward_total=(
                int(stats_map[task.id].reward_total) if task.id in stats_map else 0
            ),
            include_empty_rewards=include_empty_rewards,
            trials=None,
            jobs=(
                jobs_by_subject.get(("tasks", task.id), [])
                if jobs_by_subject is not None
                else None
            ),
            experiment_context_id=experiment_context_id,
            effective_version_id=_effective(task),
        )
        for task in tasks
    ]
