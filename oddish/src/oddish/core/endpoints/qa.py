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
    TaskVersionModel,
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
                           external_id,
                           payload
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
                          to_cancel.external_id,
                          to_cancel.payload
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
    # Also cancel per-trial ANALYSIS jobs (the trial re-run button enqueues
    # these). Left alive, one would flip the cancelled analysis back to
    # QUEUED on claim and overwrite the cancelled state.
    live_trial_ids = [
        trial.id
        for trial in task.trials or []
        if trial.superseded_by_trial_id is None
    ]
    analysis_rows = await _cancel_worker_jobs_for_kind(
        session,
        kind="ANALYSIS",
        subject_table="trials",
        subject_ids=live_trial_ids,
        reason=USER_CANCELLED_MESSAGE,
    )
    if analysis_rows:
        cancelled_trial_ids = {str(row.get("subject_id")) for row in analysis_rows}
        for trial in task.trials or []:
            if trial.id in cancelled_trial_ids and _has_active_analysis(trial):
                trial.analysis_status = AnalysisStatus.FAILED
                trial.analysis_error = USER_CANCELLED_MESSAGE
                trial.analysis_finished_at = now_value
    # An audit-only job (payload mode "pre_trial") never touches the verdict
    # or trial classifications. Cancelling one must not wipe them.
    full_qa_cancelled = any(
        ((row.get("payload") or {}) or {}).get("mode") != "pre_trial" for row in rows
    )
    if (
        full_qa_cancelled
        or _has_active_verdict(task)
        or task.status == TaskStatus.VERDICT_PENDING
    ):
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
    # The pre-trial audit runs inside a QA job (full or audit-only). A request
    # left QUEUED (or a claim left RUNNING) with no job behind it would keep
    # the card in a running state forever, so cancel always clears it.
    if task.current_version_id:
        version = await session.get(
            TaskVersionModel, task.current_version_id, with_for_update=True
        )
        if version is not None and version.pre_trial_status in (
            VerdictStatus.PENDING,
            VerdictStatus.QUEUED,
            VerdictStatus.RUNNING,
        ):
            version.pre_trial_status = VerdictStatus.FAILED
            version.pre_trial_error = USER_CANCELLED_MESSAGE
            version.pre_trial_finished_at = now_value

    await session.commit()
    return {
        "status": "cancelled",
        "task_id": task_id,
        "qa_jobs_cancelled": len(rows) + len(analysis_rows),
        **_collect_cancel_metadata([*rows, *analysis_rows]),
    }


def _reset_trial_analysis(trial: TrialModel) -> None:
    """Clear cached analysis state before re-running analysis."""
    trial.analysis = None
    trial.analysis_status = None
    trial.analysis_error = None
    trial.analysis_started_at = None
    trial.analysis_finished_at = None
    # Also drop the previous run's log, so the card never shows the old
    # run's output while the new run waits for a worker.
    trial.analysis_log = None


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
    return await backfill_task_analysis_core(
        session,
        task_id=task_id,
        org_id=org_id,
        trial_ids=None,
        force=True,
        enable_analysis=True,
    )


async def backfill_task_analysis_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
    trial_ids: list[str] | None = None,
    force: bool = False,
    enable_analysis: bool = False,
) -> dict[str, str | int]:
    """(Re)run task-level QA to backfill trial analysis.

    Resets the task verdict (so the QA job runs instead of short-circuiting
    on a terminal verdict) and enqueues one QA job. The QA job is idempotent
    at trial granularity, so:

    * ``force=False`` resets no trial analyses -> only genuinely-missing
      trials are (re)classified;
    * ``force=True`` with ``trial_ids`` resets only those trials -> true
      per-trial re-run;
    * ``force=True`` without ``trial_ids`` resets every live trial.

    ``enable_analysis=True`` also flips ``task.run_analysis`` on so future
    trials auto-analyze. Directly enqueuing the QA job is the gate override:
    the worker does not recheck ``run_analysis``.
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

    reset_count = 0
    if force:
        if trial_ids is not None:
            wanted = set(trial_ids)
            to_reset = [t for t in live_trials if t.id in wanted]
        else:
            to_reset = live_trials
        for trial in to_reset:
            _reset_trial_analysis(trial)
            reset_count += 1

    _reset_task_verdict(task)
    if enable_analysis:
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
        "reset_count": reset_count,
    }


async def rerun_pre_trial_audit_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> dict[str, str]:
    """Queue the pre-trial audit for the task's current version.

    This is the independent audit trigger. It does not classify trials and
    it does not synthesize the verdict. It is blocked only while an audit
    of this version is running inside its lease.
    """
    from datetime import timedelta

    from oddish.config import settings

    task = await session.get(TaskModel, task_id)
    if not task or (org_id is not None and task.org_id != org_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if not task.current_version_id:
        raise HTTPException(status_code=400, detail="Task has no version to audit")

    version = await session.get(
        TaskVersionModel, task.current_version_id, with_for_update=True
    )
    if version is None:
        raise HTTPException(status_code=400, detail="Task has no version to audit")

    # Mirrors the worker's claim lease in workers/queue/qa_handler.py
    # (pre_trial_timeout + PRE_TRIAL_LEASE_MARGIN_SECONDS +
    # PRE_TRIAL_LEASE_JITTER_SECONDS). Copied, not imported: importing the
    # worker module would pull the analyzer stack into the API process.
    lease = timedelta(seconds=settings.pre_trial_timeout + 900 + 60)
    if (
        version.pre_trial_status == VerdictStatus.RUNNING
        and version.pre_trial_started_at is not None
        and utcnow() - version.pre_trial_started_at < lease
    ):
        raise HTTPException(
            status_code=400,
            detail="An audit is already running for this version",
        )

    # A queued request with a live job behind it must not be queued again.
    # A stale QUEUED status with no job (cancelled or crashed job) may be:
    # re-queuing is the remedy there.
    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus

    active_audit_job = await session.scalar(
        select(WorkerJobModel.id)
        .where(
            WorkerJobModel.kind == WorkerJobKind.QA,
            WorkerJobModel.subject_table == "tasks",
            WorkerJobModel.subject_id == task_id,
            WorkerJobModel.status.in_(
                [
                    WorkerJobStatus.QUEUED,
                    WorkerJobStatus.RETRYING,
                    WorkerJobStatus.RUNNING,
                ]
            ),
            WorkerJobModel.payload["mode"].astext == "pre_trial",
        )
        .limit(1)
    )
    if active_audit_job is not None:
        raise HTTPException(
            status_code=400,
            detail="An audit job is already queued or running for this task",
        )

    # A RUNNING full QA job also runs the audit, and its trial
    # classifications read the stored findings as context. Clearing the
    # findings mid-run would feed that job mixed or empty pre-trial data.
    running_task_qa = await session.scalar(
        select(WorkerJobModel.id)
        .where(
            WorkerJobModel.kind == WorkerJobKind.QA,
            WorkerJobModel.subject_table == "tasks",
            WorkerJobModel.subject_id == task_id,
            WorkerJobModel.status == WorkerJobStatus.RUNNING,
            func.coalesce(WorkerJobModel.payload["mode"].astext, "full")
            != "pre_trial",
        )
        .limit(1)
    )
    if running_task_qa is not None:
        raise HTTPException(
            status_code=400,
            detail="Task-level QA is running; wait for it to finish",
        )

    # Reset the previous audit and queue a new one. QUEUED (not None) keeps
    # the card showing progress while the job waits for a worker.
    version.pre_trial_status = VerdictStatus.QUEUED
    version.pre_trial = None
    version.pre_trial_error = None
    version.pre_trial_started_at = None
    version.pre_trial_finished_at = None

    from oddish.queue import enqueue_pre_trial_worker_job

    await enqueue_pre_trial_worker_job(session, task_id=task.id, org_id=task.org_id)
    await session.commit()
    return {"status": "queued", "task_id": task_id}
