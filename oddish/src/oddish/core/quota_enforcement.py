from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.cost_basis import (
    CANCELLED_HARBOR_STAGE,
    not_excluded_llm_key_filter,
)
from oddish.core.helpers import HarvestTerminationError, terminate_run_harvest
from oddish.core.quotas import (
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    org_inflight_reserved_usd,
    quota_window_start,
    start_of_month_utc,
    sum_cost_usd,
    sum_org_cost_usd,
    try_acquire_quota_locks,
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
_RETRY_INITIAL_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0


class QuotaLockBusy(Exception):
    """Another transaction holds the quota advisory lock.

    Distinct from "under quota": callers that must settle only after a real
    check (``enforce_trial_quotas_until_checked``, live-tail cost acks) treat
    this like a failed check and retry, rather than proceeding as if headroom
    remained.
    """


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
    # Non-blocking: many workers call enforcement on cost checkpoints. If
    # another transaction already holds the org/user quota lock (an in-flight
    # cancellation), raise instead of pile-waiting — and instead of returning
    # None, which means under-quota.
    if not await try_acquire_quota_locks(session, org_id, billed_user_id):
        raise QuotaLockBusy(
            f"quota lock busy for org_id={org_id} billed_user_id={billed_user_id}"
        )
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
    trial_branch = (
        "(subject_table = 'trials' AND subject_id = ANY(:trial_ids))"
        if trial_ids
        else "FALSE"
    )
    task_branch = (
        "(subject_table = 'tasks' AND subject_id = ANY(:task_ids))"
        if task_ids
        else "FALSE"
    )
    params: dict[str, Any] = {"reason": QUOTA_CANCELLED_MESSAGE}
    if trial_ids:
        params["trial_ids"] = trial_ids
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
                      AND ({trial_branch} OR {task_branch})
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
        "released_trial_ids": [],
        "affected_task_ids": [],
        "cancelled_trial_ids": [],
        "quota_billed_user_id": billed_user_id,
    }
    if settings.quota_mode != QuotaMode.ENFORCE or org_id is None:
        return result

    scope = await _quota_scope_reached(session, org_id, billed_user_id)
    if scope is None:
        return result

    task_ids = (
        await session.scalars(
            select(TrialModel.task_id)
            .where(*_active_trial_predicates(org_id, billed_user_id, scope=scope))
            .distinct()
            .order_by(TrialModel.task_id)
            .execution_options(include_deleted=True)
        )
    ).all()
    if task_ids:
        await session.execute(
            select(TaskModel.id)
            .where(TaskModel.id.in_(task_ids))
            .order_by(TaskModel.id)
            .with_for_update()
        )

    trials = (
        await session.scalars(
            select(TrialModel)
            .where(*_active_trial_predicates(org_id, billed_user_id, scope=scope))
            .order_by(TrialModel.id)
            .with_for_update()
            .execution_options(include_deleted=True)
        )
    ).all()
    if not trials:
        result["scope"] = scope
        return result

    now = utcnow()
    trial_ids = [trial.id for trial in trials]
    affected_task_ids = sorted({trial.task_id for trial in trials})
    for trial in trials:
        trial.status = TrialStatus.FAILED
        trial.error_message = QUOTA_CANCELLED_MESSAGE
        trial.finished_at = now
        trial.harbor_stage = CANCELLED_HARBOR_STAGE
        trial.max_attempts = trial.attempts
        trial.current_worker_id = None
        trial.current_queue_slot = None
        if trial.analysis_status not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED):
            trial.analysis_status = AnalysisStatus.FAILED
            trial.analysis_error = QUOTA_CANCELLED_MESSAGE
            trial.analysis_finished_at = now
    await session.flush()

    result["affected_task_ids"] = affected_task_ids
    result["cancelled_trial_ids"] = trial_ids

    modal_ids, caller_modal_ids, worker_targets = await _cancel_worker_jobs(
        session,
        trial_ids=trial_ids,
        task_ids=[],
        caller_trial_id=caller_trial_id,
    )
    await session.flush()
    result.update(
        {
            "scope": scope,
            "trials_cancelled": len(trials),
            "modal_function_call_ids": modal_ids,
            "caller_modal_function_call_ids": caller_modal_ids,
            "worker_targets": worker_targets,
        }
    )
    await _reconcile_cancelled_tasks(session, result)
    return result


async def _reconcile_cancelled_tasks(
    session: AsyncSession, result: dict[str, Any]
) -> None:
    task_ids = result["affected_task_ids"]
    if not task_ids:
        return

    # Preserve the global Task -> Trial -> WorkerJob lock order.
    tasks = (
        await session.scalars(
            select(TaskModel)
            .where(TaskModel.id.in_(task_ids))
            .order_by(TaskModel.id)
            .with_for_update()
        )
    ).all()
    trials = (
        await session.scalars(
            select(TrialModel)
            .where(TrialModel.task_id.in_(task_ids))
            .order_by(TrialModel.id)
            .with_for_update()
            .execution_options(include_deleted=True)
        )
    ).all()

    scope = result["scope"]
    billed_user_id = result["quota_billed_user_id"]
    preserved_task_ids = {
        trial.task_id
        for trial in trials
        if trial.deleted_at is None
        and trial.superseded_by_trial_id is None
        and (
            (trial.finished_at is None and trial.status in _ACTIVE_TRIAL_STATUSES)
            or (
                scope == "user"
                and trial.billed_user_id is not None
                and trial.billed_user_id != billed_user_id
            )
        )
    }
    exhausted_task_ids = [
        task_id for task_id in task_ids if task_id not in preserved_task_ids
    ]

    now = utcnow()
    tasks_cancelled = 0
    exhausted = set(exhausted_task_ids)
    for task in tasks:
        if task.id not in exhausted:
            continue
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

    released_trial_ids: list[str] = []
    cancelled_trial_ids = set(result["cancelled_trial_ids"])
    preserved_cancelled_trials = [
        trial
        for trial in trials
        if trial.id in cancelled_trial_ids
        and trial.task_id in preserved_task_ids
        and trial.deleted_at is None
        and trial.superseded_by_trial_id is None
    ]
    if preserved_cancelled_trials:
        from oddish.queue import maybe_start_qa_stage, release_gate_after_quota_cancel

        for trial in preserved_cancelled_trials:
            released_trial_ids.extend(
                await release_gate_after_quota_cancel(session, trial.id)
            )
        for trial_id in {
            trial.task_id: trial.id for trial in preserved_cancelled_trials
        }.values():
            await maybe_start_qa_stage(session, trial_id)

    released_trial_ids = [
        trial_id
        for trial_id in released_trial_ids
        if trial_id not in cancelled_trial_ids
    ]
    result["released_trial_ids"] = list(
        dict.fromkeys([*result["released_trial_ids"], *released_trial_ids])
    )
    result["tasks_cancelled"] = max(result["tasks_cancelled"], tasks_cancelled)

    modal_ids, _, worker_targets = await _cancel_worker_jobs(
        session,
        trial_ids=[],
        task_ids=exhausted_task_ids,
        caller_trial_id=None,
    )
    result["modal_function_call_ids"] = list(
        dict.fromkeys([*result["modal_function_call_ids"], *modal_ids])
    )
    result["worker_targets"] = sorted(
        set(result["worker_targets"]) | set(worker_targets)
    )
    await session.flush()


async def enforce_trial_quotas(
    *,
    org_id: str | None,
    billed_user_id: str | None,
    caller_trial_id: str | None = None,
    after_check: Callable[[], Awaitable[None]] | None = None,
    after_gate_release: Callable[[list[str]], Awaitable[None]] | None = None,
) -> int | None:
    try:
        async with get_session() as session:
            result = await cancel_trials_if_quota_reached(
                session,
                org_id=org_id,
                billed_user_id=billed_user_id,
                caller_trial_id=caller_trial_id,
            )
    except QuotaLockBusy:
        # Incomplete check: do not run settlement hooks / ack live-tail cost.
        # ``enforce_trial_quotas_until_checked`` and live-tail both retry on None.
        logger.debug(
            "metric=quota.lock_busy org_id=%s billed_user_id=%s caller_trial_id=%s",
            org_id,
            billed_user_id,
            caller_trial_id,
        )
        return None
    except Exception:
        logger.exception(
            "Quota cancellation failed for org_id=%s billed_user_id=%s",
            org_id,
            billed_user_id,
        )
        return None

    try:
        if after_gate_release is not None:
            await after_gate_release(list(result["released_trial_ids"]))
        if after_check is not None:
            await after_check()
    finally:
        await _terminate_quota_harvest(
            modal_function_call_ids=result["modal_function_call_ids"],
            worker_targets=result["worker_targets"],
            caller_modal_function_call_ids=result["caller_modal_function_call_ids"],
            org_id=org_id,
            billed_user_id=billed_user_id,
        )

    if result["trials_cancelled"]:
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


async def _terminate_quota_harvest(
    *,
    modal_function_call_ids: list[str],
    worker_targets: list[tuple[str, str]],
    caller_modal_function_call_ids: list[str],
    org_id: str | None,
    billed_user_id: str | None,
) -> None:
    for modal_ids, targets in (
        (modal_function_call_ids, worker_targets),
        (caller_modal_function_call_ids, []),
    ):
        if not modal_ids and not targets:
            continue
        pending_modal_ids = modal_ids
        pending_targets = targets
        retry_delay = _RETRY_INITIAL_SECONDS
        while True:
            try:
                await terminate_run_harvest(
                    {
                        "modal_function_call_ids": pending_modal_ids,
                        "worker_targets": pending_targets,
                    },
                    strict=True,
                )
                break
            except HarvestTerminationError as exc:
                pending_modal_ids = exc.modal_function_call_ids
                pending_targets = exc.worker_targets
                logger.exception(
                    "Quota remote termination incomplete for org_id=%s "
                    "billed_user_id=%s",
                    org_id,
                    billed_user_id,
                )
            except Exception:
                logger.exception(
                    "Quota remote termination failed for org_id=%s billed_user_id=%s",
                    org_id,
                    billed_user_id,
                )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _RETRY_MAX_SECONDS)


async def enforce_trial_quotas_until_checked(
    *,
    org_id: str | None,
    billed_user_id: str | None,
    caller_trial_id: str | None = None,
    after_check: Callable[[], Awaitable[None]] | None = None,
    after_gate_release: Callable[[list[str]], Awaitable[None]] | None = None,
) -> int:
    if settings.quota_mode != QuotaMode.ENFORCE or org_id is None:
        if after_check is not None:
            await after_check()
        return 0
    retry_delay = _RETRY_INITIAL_SECONDS
    while True:
        cancelled = await enforce_trial_quotas(
            org_id=org_id,
            billed_user_id=billed_user_id,
            caller_trial_id=caller_trial_id,
            after_check=after_check,
            after_gate_release=after_gate_release,
        )
        if cancelled is not None:
            return cancelled
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, _RETRY_MAX_SECONDS)
