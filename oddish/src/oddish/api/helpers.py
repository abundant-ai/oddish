from __future__ import annotations

import json
from typing import Sequence

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import TaskModel, TaskStatus, TrialModel, TrialStatus
from oddish.model_pricing import estimate_cost_usd
from oddish.schemas import TaskStatusResponse, TrialResponse

_ANALYSIS_SUMMARY_UNSET = object()


def _resolve_cost(
    trial: TrialModel, model_name: str | None
) -> tuple[float | None, bool | None, str | None]:
    """Return (cost_usd, cost_is_estimated, cost_estimation_method) for a trial.

    If the trial already has a native cost_usd, returns it as-is.
    Otherwise estimates from tokens × pricing table if possible.
    """
    if trial.cost_usd is not None:
        return trial.cost_usd, False, "native"

    # No native cost — try to estimate from token counts
    if trial.input_tokens is not None or trial.output_tokens is not None:
        estimated = estimate_cost_usd(
            model_name,
            trial.input_tokens or 0,
            trial.output_tokens or 0,
            trial.cache_tokens,
        )
        if estimated is not None:
            return estimated, True, "estimated_from_tokens"

    return None, None, None


def build_trial_response(trial: TrialModel, task_path: str) -> TrialResponse:
    """Build a TrialResponse from a TrialModel."""
    normalized_model = settings.normalize_trial_model(trial.agent, trial.model)
    cost_usd, cost_is_estimated, cost_estimation_method = _resolve_cost(
        trial, normalized_model or trial.model
    )
    return TrialResponse(
        id=trial.id,
        name=trial.name,
        task_id=trial.task_id,
        task_path=task_path,
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
        cost_usd=cost_usd,
        cost_is_estimated=cost_is_estimated,
        cost_estimation_method=cost_estimation_method,
        phase_timing=trial.phase_timing,
        has_trajectory=trial.has_trajectory,
        analysis_status=trial.analysis_status,
        analysis=trial.analysis,
        analysis_error=trial.analysis_error,
        created_at=trial.created_at,
        started_at=trial.started_at,
        finished_at=trial.finished_at,
    )


def build_compact_trial_response(
    trial: TrialModel,
    task_path: str,
    *,
    analysis_summary: dict[str, str | None] | None | object = _ANALYSIS_SUMMARY_UNSET,
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

    return TrialResponse(
        id=trial.id,
        name=trial.name,
        task_id=trial.task_id,
        task_path=task_path,
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
        cost_is_estimated=None,
        cost_estimation_method=None,
        phase_timing=trial.phase_timing,
        has_trajectory=trial.has_trajectory,
        analysis_status=trial.analysis_status,
        analysis=resolved_analysis_summary,
        analysis_error=None,
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
    task: TaskModel, *, include_empty_rewards: bool = True
) -> TaskStatusResponse:
    """Build a TaskStatusResponse from a TaskModel with eagerly loaded trials."""
    total = len(task.trials)
    completed = sum(1 for t in task.trials if t.status == TrialStatus.SUCCESS)
    failed = sum(1 for t in task.trials if t.status == TrialStatus.FAILED)
    reward_success = sum(1 for t in task.trials if t.reward == 1)
    reward_total = sum(1 for t in task.trials if t.reward is not None)
    trials = [build_trial_response(t, task.task_path) for t in task.trials]

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
