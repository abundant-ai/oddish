from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.cost_basis import not_excluded_llm_key_filter
from oddish.core.helpers import terminate_run_harvest
from oddish.core.quotas import (
    acquire_quota_locks,
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    org_inflight_reserved_usd,
    quota_window_start,
    start_of_month_utc,
    sum_cost_usd,
    sum_org_cost_usd,
)
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
    utcnow,
)

logger = logging.getLogger(__name__)

QUOTA_CANCELLED_MESSAGE = "Cancelled because quota was reached"

_ACTIVE_TRIAL_STATUSES = (
    TrialStatus.PENDING,
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
)
_ACTIVE_TASK_STATUSES = (
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.ANALYZING,
    TaskStatus.VERDICT_PENDING,
)


async def _quota_scope_reached(
    session: AsyncSession,
    org_id: str,
    billed_user_id: str | None,
) -> str | None:
    await acquire_quota_locks(session, org_id, billed_user_id)
    org_limit = await get_effective_org_limit(session, org_id)
    if org_limit is not None:
        org_used = await sum_org_cost_usd(session, org_id, start_of_month_utc())
        org_reserved = await org_inflight_reserved_usd(session, org_id)
        if org_used + org_reserved >= org_limit:
            return "org"

    if billed_user_id is None:
        return None

    user_limit = await get_effective_limit(session, org_id, billed_user_id)
    user_used = await sum_cost_usd(
        session, org_id, billed_user_id, quota_window_start()
    )
    user_reserved = await inflight_reserved_usd(session, org_id, billed_user_id)
    return "user" if user_used + user_reserved >= user_limit else None


def _active_trial_predicates(
    org_id: str, billed_user_id: str | None, *, scope: str
) -> list:
    predicates = [
        TrialModel.org_id == org_id,
        TrialModel.finished_at.is_(None),
        TrialModel.deleted_at.is_(None),
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.status.in_(_ACTIVE_TRIAL_STATUSES),
        not_excluded_llm_key_filter(),
    ]
    if scope == "user":
        predicates.append(TrialModel.billed_user_id == billed_user_id)
    return predicates


async def _cancel_worker_jobs(
    session: AsyncSession,
    *,
    trial_ids: list[str],
    task_ids: list[str],
    caller_trial_id: str | None,
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    task_branch = (
        "(subject_table = 'tasks' AND subject_id = ANY(:task_ids))"
        if task_ids
        else "FALSE"
    )
    params: dict[str, Any] = {
        "reason": QUOTA_CANCELLED_MESSAGE,
        "trial_ids": trial_ids,
    }
    if task_ids:
        params["task_ids"] = task_ids
    rows = (
        await session.execute(
            text(
                f"""
                WITH to_cancel AS (
                    SELECT id, subject_table, subject_id,
                           modal_function_call_id, provider, external_id
                    FROM worker_jobs
                    WHERE status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                      AND ((subject_table = 'trials' AND subject_id = ANY(:trial_ids))
                           OR {task_branch})
                    ORDER BY id
                    FOR UPDATE
                )
                UPDATE worker_jobs AS w
                SET status = 'CANCELLED',
                    finished_at = NOW(),
                    error_message = :reason,
                    current_worker_id = NULL,
                    current_queue_slot = NULL,
                    modal_function_call_id = NULL,
                    payload = w.payload - 'registry_auth_enc'
                FROM to_cancel
                WHERE w.id = to_cancel.id
                RETURNING to_cancel.subject_table,
                          to_cancel.subject_id,
                          to_cancel.modal_function_call_id,
                          to_cancel.provider,
                          to_cancel.external_id
                """
            ),
            params,
        )
    ).all()
    caller_modal_ids = list(
        dict.fromkeys(
            str(row[2])
            for row in rows
            if row[0] == "trials" and row[1] == caller_trial_id and row[2] is not None
        )
    )
    modal_ids = list(
        dict.fromkeys(
            str(row[2])
            for row in rows
            if row[2] is not None and str(row[2]) not in caller_modal_ids
        )
    )
    worker_targets = sorted(
        {
            (str(row[3]), str(row[4]))
            for row in rows
            if row[3] is not None and row[4] is not None
        }
    )
    return modal_ids, caller_modal_ids, worker_targets


async def cancel_trials_if_quota_reached(
    session: AsyncSession,
    *,
    org_id: str | None,
    billed_user_id: str | None,
    caller_trial_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scope": None,
        "trials_cancelled": 0,
        "tasks_cancelled": 0,
        "modal_function_call_ids": [],
        "caller_modal_function_call_ids": [],
        "worker_targets": [],
    }
    if settings.quota_mode != QuotaMode.ENFORCE or org_id is None:
        return result

    scope = await _quota_scope_reached(session, org_id, billed_user_id)
    if scope is None:
        return result

    trials = list(
        (
            await session.execute(
                select(TrialModel)
                .where(*_active_trial_predicates(org_id, billed_user_id, scope=scope))
                .order_by(TrialModel.id)
                .with_for_update()
                .execution_options(include_deleted=True)
            )
        )
        .scalars()
        .all()
    )
    if not trials:
        result["scope"] = scope
        return result

    now = utcnow()
    trial_ids = [trial.id for trial in trials]
    affected_task_ids = list(dict.fromkeys(trial.task_id for trial in trials))
    for trial in trials:
        trial.status = TrialStatus.FAILED
        trial.error_message = QUOTA_CANCELLED_MESSAGE
        trial.finished_at = now
        trial.harbor_stage = "cancelled"
        trial.max_attempts = trial.attempts
        trial.current_worker_id = None
        trial.current_queue_slot = None
        if trial.analysis_status not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED):
            trial.analysis_status = AnalysisStatus.FAILED
            trial.analysis_error = QUOTA_CANCELLED_MESSAGE
            trial.analysis_finished_at = now
    await session.flush()

    tasks_with_live_trials = set(
        (
            await session.execute(
                select(TrialModel.task_id)
                .where(
                    TrialModel.task_id.in_(affected_task_ids),
                    TrialModel.finished_at.is_(None),
                    TrialModel.deleted_at.is_(None),
                    TrialModel.superseded_by_trial_id.is_(None),
                    TrialModel.status.in_(_ACTIVE_TRIAL_STATUSES),
                )
                .execution_options(include_deleted=True)
            )
        )
        .scalars()
        .all()
    )
    exhausted_task_ids = [
        task_id
        for task_id in affected_task_ids
        if task_id not in tasks_with_live_trials
    ]
    tasks_cancelled = 0
    if exhausted_task_ids:
        tasks = list(
            (
                await session.execute(
                    select(TaskModel)
                    .where(TaskModel.id.in_(exhausted_task_ids))
                    .order_by(TaskModel.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for task in tasks:
            if task.status in _ACTIVE_TASK_STATUSES:
                task.status = TaskStatus.FAILED
                task.finished_at = now
                tasks_cancelled += 1
            if task.verdict_status in (
                VerdictStatus.PENDING,
                VerdictStatus.QUEUED,
                VerdictStatus.RUNNING,
            ):
                task.verdict_status = VerdictStatus.FAILED
                task.verdict_error = QUOTA_CANCELLED_MESSAGE
                task.verdict_finished_at = now

    modal_ids, caller_modal_ids, worker_targets = await _cancel_worker_jobs(
        session,
        trial_ids=trial_ids,
        task_ids=exhausted_task_ids,
        caller_trial_id=caller_trial_id,
    )
    await session.flush()
    return {
        "scope": scope,
        "trials_cancelled": len(trials),
        "tasks_cancelled": tasks_cancelled,
        "modal_function_call_ids": modal_ids,
        "caller_modal_function_call_ids": caller_modal_ids,
        "worker_targets": worker_targets,
    }


async def enforce_trial_quotas(
    *,
    org_id: str | None,
    billed_user_id: str | None,
    caller_trial_id: str | None = None,
) -> int | None:
    try:
        async with get_session() as session:
            result = await cancel_trials_if_quota_reached(
                session,
                org_id=org_id,
                billed_user_id=billed_user_id,
                caller_trial_id=caller_trial_id,
            )
    except Exception:
        logger.exception(
            "Quota cancellation failed for org_id=%s billed_user_id=%s",
            org_id,
            billed_user_id,
        )
        return None

    if result["trials_cancelled"]:
        caller_modal_ids = result.pop("caller_modal_function_call_ids")
        try:
            await terminate_run_harvest(result)
            if caller_modal_ids:
                await terminate_run_harvest(
                    {
                        "modal_function_call_ids": caller_modal_ids,
                        "worker_targets": [],
                    }
                )
        except Exception:
            logger.exception(
                "Quota remote termination failed for org_id=%s billed_user_id=%s",
                org_id,
                billed_user_id,
            )
        logger.warning(
            "metric=quota.trials_cancelled scope=%s org_id=%s "
            "billed_user_id=%s trials=%s tasks=%s",
            result["scope"],
            org_id,
            billed_user_id,
            result["trials_cancelled"],
            result["tasks_cancelled"],
        )
    return int(result["trials_cancelled"])
