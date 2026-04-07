from __future__ import annotations

import heapq
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import Priority, TaskModel, TaskStatus, TrialModel, TrialStatus
from oddish.schemas import TaskStatusResponse, TrialQueueInfo, TrialResponse

_ANALYSIS_SUMMARY_UNSET = object()
_QUEUE_PENDING_STATUSES = {TrialStatus.QUEUED, TrialStatus.RETRYING}
_QUEUE_ACTIVE_STATUSES = _QUEUE_PENDING_STATUSES | {TrialStatus.RUNNING}


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
    queued_trials = [trial for trial in trials if trial.status in _QUEUE_PENDING_STATUSES]
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


def build_trial_response(
    trial: TrialModel,
    task_path: str,
    *,
    queue_info: TrialQueueInfo | None = None,
) -> TrialResponse:
    """Build a TrialResponse from a TrialModel."""
    normalized_model = settings.normalize_trial_model(trial.agent, trial.model)
    task_version, task_version_id = _resolve_trial_version_fields(trial)
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
        attempts=trial.attempts,
        max_attempts=trial.max_attempts,
        harbor_stage=trial.harbor_stage,
        reward=trial.reward,
        error_message=trial.error_message,
        result=trial.result,
        input_tokens=trial.input_tokens,
        cache_tokens=trial.cache_tokens,
        output_tokens=trial.output_tokens,
        cost_usd=trial.cost_usd,
        phase_timing=trial.phase_timing,
        has_trajectory=trial.has_trajectory,
        analysis_status=trial.analysis_status,
        analysis=trial.analysis,
        analysis_error=trial.analysis_error,
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
        attempts=trial.attempts,
        max_attempts=trial.max_attempts,
        harbor_stage=trial.harbor_stage,
        reward=trial.reward,
        error_message=trial.error_message,
        result=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=trial.phase_timing,
        has_trajectory=trial.has_trajectory,
        analysis_status=trial.analysis_status,
        analysis=resolved_analysis_summary,
        analysis_error=None,
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
    reward_total: int,
    include_empty_rewards: bool,
) -> tuple[int | None, int | None]:
    if include_empty_rewards or reward_total > 0:
        return reward_success, reward_total
    return None, None


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


def _resolve_task_version_fields(
    task: TaskModel,
) -> tuple[int | None, str | None]:
    """Extract current version number and id from a task."""
    version_id = task.current_version_id
    if version_id is None:
        return None, None
    parts = version_id.rsplit("-v", 1)
    version_number = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
    return version_number, version_id


def _build_task_status_response(
    task: TaskModel,
    *,
    total: int,
    completed: int,
    failed: int,
    reward_success: int,
    reward_total: int,
    include_empty_rewards: bool,
    trials: list[TrialResponse] | None,
) -> TaskStatusResponse:
    formatted_reward_success, formatted_reward_total = _format_reward_fields(
        reward_success=reward_success,
        reward_total=reward_total,
        include_empty_rewards=include_empty_rewards,
    )
    current_version, current_version_id = _resolve_task_version_fields(task)
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
        experiment_id=task.experiment_id,
        experiment_name=task.experiment.name,
        experiment_is_public=task.experiment.is_public if task.experiment else False,
        current_version=current_version,
        current_version_id=current_version_id,
        total=total,
        completed=completed,
        failed=failed,
        progress=f"{completed}/{total} completed",
        trials=trials,
        reward_success=formatted_reward_success,
        reward_total=formatted_reward_total,
        run_analysis=task.run_analysis,
        verdict_status=task.verdict_status,
        verdict=task.verdict,
        verdict_error=task.verdict_error,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def build_task_status_response(
    task: TaskModel,
    *,
    include_empty_rewards: bool = True,
    queue_info_by_trial_id: dict[str, TrialQueueInfo] | None = None,
) -> TaskStatusResponse:
    """Build a TaskStatusResponse from a TaskModel with eagerly loaded trials."""
    total = len(task.trials)
    completed = sum(1 for t in task.trials if t.status == TrialStatus.SUCCESS)
    failed = sum(1 for t in task.trials if t.status == TrialStatus.FAILED)
    reward_success = sum(1 for t in task.trials if t.reward == 1)
    reward_total = sum(1 for t in task.trials if t.reward is not None)
    trials = [
        build_trial_response(
            t,
            task.task_path,
            queue_info=(
                queue_info_by_trial_id.get(t.id)
                if queue_info_by_trial_id is not None
                else None
            ),
        )
        for t in task.trials
    ]

    return _build_task_status_response(
        task,
        total=total,
        completed=completed,
        failed=failed,
        reward_success=reward_success,
        reward_total=reward_total,
        include_empty_rewards=include_empty_rewards,
        trials=trials,
    )


def build_task_status_response_compact(
    task: TaskModel,
    *,
    include_empty_rewards: bool = True,
    analysis_summaries: dict[str, dict[str, str | None]] | None = None,
    queue_info_by_trial_id: dict[str, TrialQueueInfo] | None = None,
) -> TaskStatusResponse:
    """Build TaskStatusResponse with compact per-trial payloads."""
    total = len(task.trials)
    completed = sum(1 for t in task.trials if t.status == TrialStatus.SUCCESS)
    failed = sum(1 for t in task.trials if t.status == TrialStatus.FAILED)
    reward_success = sum(1 for t in task.trials if t.reward == 1)
    reward_total = sum(1 for t in task.trials if t.reward is not None)
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
        )
        for t in task.trials
    ]

    return _build_task_status_response(
        task,
        total=total,
        completed=completed,
        failed=failed,
        reward_success=reward_success,
        reward_total=reward_total,
        include_empty_rewards=include_empty_rewards,
        trials=trials,
    )


async def fetch_trial_analysis_summaries(
    session: AsyncSession, *, task_ids: Sequence[str]
) -> dict[str, dict[str, str | None]]:
    """Fetch only compact analysis fields needed by matrix views."""
    if not task_ids:
        return {}

    result = await session.execute(
        select(
            TrialModel.id,
            TrialModel.analysis["classification"].astext.label("classification"),
            TrialModel.analysis["subtype"].astext.label("subtype"),
            TrialModel.analysis["evidence"].astext.label("evidence"),
        ).where(
            TrialModel.task_id.in_(task_ids),
            TrialModel.analysis.isnot(None),
        )
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
) -> list[TaskStatusResponse]:
    """Build TaskStatusResponse objects with aggregated trial counts."""
    if not tasks:
        return []

    task_ids = [task.id for task in tasks]
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
            func.count(case((TrialModel.reward.isnot(None), 1))).label("reward_total"),
        )
        .where(TrialModel.task_id.in_(task_ids))
        .group_by(TrialModel.task_id)
    )

    stats_result = await session.execute(stats_query)
    stats_map = {row.task_id: row for row in stats_result.all()}

    return [
        _build_task_status_response(
            task,
            total=int(stats_map[task.id].total) if task.id in stats_map else 0,
            completed=int(stats_map[task.id].completed) if task.id in stats_map else 0,
            failed=int(stats_map[task.id].failed) if task.id in stats_map else 0,
            reward_success=(
                int(stats_map[task.id].reward_success) if task.id in stats_map else 0
            ),
            reward_total=(
                int(stats_map[task.id].reward_total) if task.id in stats_map else 0
            ),
            include_empty_rewards=include_empty_rewards,
            trials=None,
        )
        for task in tasks
    ]
