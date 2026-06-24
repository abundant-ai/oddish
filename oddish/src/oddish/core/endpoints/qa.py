from __future__ import annotations

from collections.abc import Collection

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.endpoints._common import (
    USER_CANCELLED_MESSAGE,
    _ACTIVE_WORKER_JOB_STATUSES_SQL,
    _reset_task_verdict,
)
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    utcnow,
)


def _collect_cancel_metadata(rows: Collection[object]) -> dict[str, list[str]]:
    modal_fc_ids: list[str] = []
    for row in rows:
        get = getattr(row, "get", None)
        fc = get("modal_function_call_id") if get else None
        if fc:
            modal_fc_ids.append(str(fc))
    return {"modal_function_call_ids": list(dict.fromkeys(modal_fc_ids))}


async def _cancel_worker_jobs_for_kind(
    session: AsyncSession,
    *,
    kind: str,
    subject_table: str,
    subject_ids: Collection[str],
    reason: str,
):
    if not subject_ids:
        return []
    rows = (
        (
            await session.execute(
                text(
                    f"""
                WITH to_cancel AS (
                    SELECT id,
                           modal_function_call_id,
                           provider,
                           external_id
                    FROM   worker_jobs
                    WHERE  kind::text = :kind
                      AND  subject_table = :subject_table
                      AND  subject_id = ANY(:subject_ids)
                      AND  status::text IN ({_ACTIVE_WORKER_JOB_STATUSES_SQL})
                    FOR UPDATE
                )
                UPDATE worker_jobs AS w
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = :reason,
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                FROM   to_cancel
                WHERE  w.id = to_cancel.id
                RETURNING w.id,
                          w.subject_id,
                          to_cancel.modal_function_call_id,
                          to_cancel.provider,
                          to_cancel.external_id
                """
                ),
                {
                    "kind": kind,
                    "subject_table": subject_table,
                    "subject_ids": list(dict.fromkeys(subject_ids)),
                    "reason": reason,
                },
            )
        )
        .mappings()
        .all()
    )
    return rows


def _has_active_analysis(trial: TrialModel) -> bool:
    return trial.analysis_status in (
        AnalysisStatus.PENDING,
        AnalysisStatus.QUEUED,
        AnalysisStatus.RUNNING,
    )


def _has_active_verdict(task: TaskModel) -> bool:
    return task.verdict_status in (
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    )


async def cancel_task_qa_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> dict[str, str | int | list[str]]:
    """Cancel a task's in-flight QA job.

    There is one task-level QA job: it classifies every trial and then
    synthesizes the verdict. Cancelling it stops that job and finalizes any
    trial whose classification was mid-flight (left RUNNING by a killed
    worker).
    """
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task or (org_id is not None and task.org_id != org_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    now_value = utcnow()
    rows = await _cancel_worker_jobs_for_kind(
        session,
        kind="QA",
        subject_table="tasks",
        subject_ids=[task_id],
        reason=USER_CANCELLED_MESSAGE,
    )
    if rows or _has_active_verdict(task) or task.status == TaskStatus.VERDICT_PENDING:
        task.verdict_status = VerdictStatus.FAILED
        task.verdict_error = USER_CANCELLED_MESSAGE
        task.verdict_finished_at = now_value
        # Finalize trials whose classification the QA job had in flight so
        # they don't linger in a RUNNING analysis state.
        for trial in task.trials or []:
            if trial.superseded_by_trial_id is None and _has_active_analysis(trial):
                trial.analysis_status = AnalysisStatus.FAILED
                trial.analysis_error = USER_CANCELLED_MESSAGE
                trial.analysis_finished_at = now_value
        if task.status == TaskStatus.VERDICT_PENDING:
            task.status = TaskStatus.FAILED
            task.finished_at = now_value

    await session.commit()
    return {
        "status": "cancelled",
        "task_id": task_id,
        "qa_jobs_cancelled": len(rows),
        **_collect_cancel_metadata(rows),
    }


def _reset_trial_analysis(trial: TrialModel) -> None:
    """Clear cached analysis state before re-running analysis."""
    trial.analysis = None
    trial.analysis_status = None
    trial.analysis_error = None
    trial.analysis_started_at = None
    trial.analysis_finished_at = None


async def _count_active_trials(session: AsyncSession, *, task_id: str) -> int:
    """Count non-terminal, non-superseded trials for a task."""
    active_statuses = [
        TrialStatus.PENDING,
        TrialStatus.QUEUED,
        TrialStatus.RUNNING,
        TrialStatus.RETRYING,
    ]
    count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            TrialModel.task_id == task_id,
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.status.in_(active_statuses),
        )
    )
    return int(count or 0)


async def rerun_task_qa_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> dict[str, str | int]:
    """(Re)run the single task-level QA job for a finished task.

    Resets every live trial's classification and the task verdict, then
    enqueues one QA job that re-classifies all live trials and synthesizes a
    fresh verdict.
    """
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if org_id is not None and task.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not task.trials:
        raise HTTPException(status_code=400, detail="Task has no trials to QA")

    live_trials = [
        trial for trial in task.trials if trial.superseded_by_trial_id is None
    ]
    if not live_trials:
        raise HTTPException(status_code=400, detail="Task has no live trials to QA")

    active_trials = await _count_active_trials(session, task_id=task.id)
    if active_trials > 0:
        raise HTTPException(
            status_code=400,
            detail="Can only run QA after all trials finish",
        )

    if any(
        trial.analysis_status
        in (AnalysisStatus.PENDING, AnalysisStatus.QUEUED, AnalysisStatus.RUNNING)
        for trial in live_trials
    ):
        raise HTTPException(
            status_code=400,
            detail="QA is already in progress for this task",
        )

    if task.verdict_status in (
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    ):
        raise HTTPException(
            status_code=400,
            detail="QA is already in progress for this task",
        )

    for trial in live_trials:
        _reset_trial_analysis(trial)

    _reset_task_verdict(task)
    task.run_analysis = True
    task.status = TaskStatus.VERDICT_PENDING
    task.finished_at = None
    task.verdict_status = VerdictStatus.QUEUED

    from oddish.queue import enqueue_qa_worker_job

    await enqueue_qa_worker_job(session, task_id=task.id, org_id=task.org_id)

    await session.commit()
    return {
        "status": "queued",
        "task_id": task_id,
        "trial_count": len(live_trials),
    }
