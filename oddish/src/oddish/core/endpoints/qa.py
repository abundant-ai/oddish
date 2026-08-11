from __future__ import annotations

from collections.abc import Collection

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.endpoints._common import (
    USER_CANCELLED_MESSAGE,
    _ACTIVE_WORKER_JOB_STATUSES_SQL,
)
from oddish.core.verdict_state import cancel_verdict, queue_verdict
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


async def _live_audit_trial_id(session: AsyncSession, task_id: str) -> str | None:
    return await session.scalar(
        select(TrialModel.id)
        .where(
            TrialModel.task_id == task_id,
            TrialModel.kind == "audit",
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.status.in_(
                [
                    TrialStatus.PENDING,
                    TrialStatus.QUEUED,
                    TrialStatus.RUNNING,
                    TrialStatus.RETRYING,
                ]
            ),
        )
        .limit(1)
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
    # The same task row lock the backfill and the reruns take: without it, a
    # cancel can interleave with their check-and-enqueue and either kill a
    # just-committed job's state or write verdict resets over a fresh enqueue.
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
        .with_for_update(of=TaskModel)
    )
    task = result.scalar_one_or_none()
    if not task or (org_id is not None and task.org_id != org_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    now_value = utcnow()
    # QA and audit run as trials. Cancel their TRIAL worker jobs and mark
    # the trial rows failed, the same way the append path supersedes an
    # in-flight qa trial. The settlement path and importer skip on the
    # cancelled harbor_stage.
    analysis_trials = [
        trial
        for trial in task.trials or []
        if trial.superseded_by_trial_id is None
        and (trial.kind or "agent") in ("qa", "audit")
        and trial.status
        not in (TrialStatus.SUCCESS, TrialStatus.FAILED, TrialStatus.SKIPPED)
    ]
    rows = await _cancel_worker_jobs_for_kind(
        session,
        kind="TRIAL",
        subject_table="trials",
        subject_ids=[trial.id for trial in analysis_trials],
        reason=USER_CANCELLED_MESSAGE,
    )
    for trial in analysis_trials:
        trial.status = TrialStatus.FAILED
        trial.harbor_stage = "cancelled"
        trial.error_message = USER_CANCELLED_MESSAGE
        trial.finished_at = trial.finished_at or now_value
    if (
        analysis_trials
        or _has_active_verdict(task)
        or task.status == TaskStatus.VERDICT_PENDING
    ):
        cancel_verdict(task, error=USER_CANCELLED_MESSAGE, now=now_value)
        # Finalize trials whose classification the QA job had in flight so
        # they don't linger in a RUNNING analysis state.
        for trial in task.trials or []:
            if trial.superseded_by_trial_id is None and _has_active_analysis(trial):
                trial.analysis_status = AnalysisStatus.FAILED
                trial.analysis_error = USER_CANCELLED_MESSAGE
                trial.analysis_finished_at = now_value
        if task.status == TaskStatus.VERDICT_PENDING:
            # A restored verdict means the task is judged; only a task with
            # no verdict fails on cancel.
            task.status = (
                TaskStatus.COMPLETED
                if task.verdict_status == VerdictStatus.SUCCESS
                else TaskStatus.FAILED
            )
            task.finished_at = now_value
    # A pre-trial status left QUEUED/RUNNING with nothing behind it would
    # keep the card in a running state forever, so cancel always clears it.
    # An audit trial pins the version it audits, and that version can be
    # older than the current one after a re-upload — clear it as well.
    version_ids: set[str] = set()
    if task.current_version_id:
        version_ids.add(str(task.current_version_id))
    for trial in analysis_trials:
        if trial.kind == "audit" and trial.task_version_id:
            version_ids.add(str(trial.task_version_id))
    for version_id in version_ids:
        version = await session.get(TaskVersionModel, version_id, with_for_update=True)
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
    # Also drop the previous run's log, so the card never shows the old
    # run's output while the new run waits for a worker.
    trial.analysis_log = None


async def _count_active_trials(session: AsyncSession, *, task_id: str) -> int:
    """Count non-terminal, non-superseded agent trials for a task."""
    active_statuses = [
        TrialStatus.PENDING,
        TrialStatus.QUEUED,
        TrialStatus.RUNNING,
        TrialStatus.RETRYING,
    ]
    count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            TrialModel.task_id == task_id,
            TrialModel.kind == "agent",
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

    Resets every live trial's classification, then enqueues one QA job that
    re-classifies all live trials and synthesizes a fresh verdict. The current
    published verdict remains visible until that job replaces it.
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

    Queues a replacement verdict without withdrawing the published result.
    The QA job is idempotent at trial granularity, so:

    * ``force=False`` resets no trial analyses -> only genuinely-missing
      trials are (re)classified;
    * ``force=True`` with ``trial_ids`` resets only those trials -> true
      per-trial re-run;
    * ``force=True`` without ``trial_ids`` resets every live trial.

    ``enable_analysis=True`` also flips ``task.run_analysis`` on so future
    trials auto-analyze. Directly enqueuing the QA job is the gate override:
    the worker does not recheck ``run_analysis``.
    """
    # The task row lock serializes this check-and-enqueue against the audit
    # rerun (which takes the same lock): without it, two concurrent requests
    # can each pass the job guards below and enqueue conflicting jobs.
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
        .with_for_update(of=TaskModel)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if org_id is not None and task.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not task.trials:
        raise HTTPException(status_code=400, detail="Task has no trials to QA")

    live_trials = [
        trial
        for trial in task.trials
        if trial.superseded_by_trial_id is None and (trial.kind or "agent") == "agent"
    ]
    if not live_trials:
        raise HTTPException(status_code=400, detail="Task has no live trials to QA")

    active_trials = await _count_active_trials(session, task_id=task.id)
    if active_trials > 0:
        raise HTTPException(
            status_code=400,
            detail="Can only run QA after all trials finish",
        )

    # The live qa trial IS the in-progress marker. Old status flags
    # (verdict_status, per-trial analysis_status) can be stale after a
    # crash and must not wedge the rerun button.
    live_qa = await session.scalar(
        select(TrialModel.id)
        .where(
            TrialModel.task_id == task_id,
            TrialModel.kind == "qa",
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.status.in_(
                [
                    TrialStatus.PENDING,
                    TrialStatus.QUEUED,
                    TrialStatus.RUNNING,
                    TrialStatus.RETRYING,
                ]
            ),
        )
        .limit(1)
    )
    if live_qa is not None:
        raise HTTPException(
            status_code=400,
            detail="QA is already in progress for this task",
        )

    # The QA brief embeds the audit findings. Starting QA while an audit
    # trial is live would read mixed findings.
    if await _live_audit_trial_id(session, task_id):
        raise HTTPException(
            status_code=400,
            detail="A pre-trial audit is queued or running; wait for it to finish",
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

    if enable_analysis:
        task.run_analysis = True
    task.status = TaskStatus.VERDICT_PENDING
    task.finished_at = None
    queue_verdict(task)

    from oddish.queue import qa_eligible_trial_ids
    from oddish.workers.analysis_trials import create_qa_trial

    eligible = await qa_eligible_trial_ids(session, task.id)
    if eligible:
        await create_qa_trial(session, task=task, eligible_trial_ids=eligible)
    else:
        task.status = TaskStatus.COMPLETED
        task.finished_at = utcnow()
        task.verdict_status = None

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


    # The task row lock serializes this check-and-enqueue against the QA
    # backfill (which takes the same lock): without it, two concurrent
    # requests can each pass the job guards below and enqueue conflicting
    # jobs.
    task = await session.get(TaskModel, task_id, with_for_update=True)
    if not task or (org_id is not None and task.org_id != org_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if not task.current_version_id:
        raise HTTPException(status_code=400, detail="Task has no version to audit")

    version = await session.get(
        TaskVersionModel, task.current_version_id, with_for_update=True
    )
    if version is None:
        raise HTTPException(status_code=400, detail="Task has no version to audit")

    # The audit trial's own timeout bounds a live run; block a re-request
    # only while one is plausibly in flight.
    from oddish.workers.analysis_trials import ANALYSIS_TRIAL_TIMEOUT_MINUTES

    lease = timedelta(minutes=ANALYSIS_TRIAL_TIMEOUT_MINUTES * 2)
    if (
        version.pre_trial_status in (VerdictStatus.QUEUED, VerdictStatus.RUNNING)
        and version.pre_trial_started_at is not None
        and utcnow() - version.pre_trial_started_at < lease
    ):
        raise HTTPException(
            status_code=400,
            detail="An audit is already running for this version",
        )

    # A queued request with a live audit trial behind it must not be queued
    # again. A stale QUEUED status with no trial (cancelled or crashed) may
    # be: re-queuing is the remedy there.
    if await _live_audit_trial_id(session, task_id):
        raise HTTPException(
            status_code=400,
            detail="An audit trial is already queued or running for this task",
        )

    # A live full QA job is not a blocker: it runs its own audit of the same
    # source, so both jobs converge on equivalent findings. If its verdict
    # reads the findings inside the brief cleared window below, it reports
    # them as unavailable rather than inventing a pass.

    # Reset the previous audit and queue a new one. QUEUED (not None) keeps
    # the card showing progress while the trial waits for a worker.
    version.pre_trial_status = VerdictStatus.QUEUED
    version.pre_trial = None
    version.pre_trial_error = None
    version.pre_trial_started_at = utcnow()
    version.pre_trial_finished_at = None

    from oddish.workers.analysis_trials import build_audit_brief, create_analysis_trial

    await create_analysis_trial(
        session,
        task=task,
        kind="audit",
        brief=build_audit_brief(task_name=task.name),
        task_version_id=str(version.id),
    )
    await session.commit()
    return {"status": "queued", "task_id": task_id}
