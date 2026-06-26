from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints._common import (
    get_trial_for_org_core,
    _reset_task_verdict,
)
from oddish.core.helpers import (
    build_trial_response,
    fetch_trial_queue_info,
    fetch_visible_worker_jobs,
)
from oddish.core.trial_io import (
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from oddish.db import (
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    utcnow,
)
from oddish.schemas import TrialResponse


async def get_trial_by_index_core(
    session: AsyncSession,
    *,
    task_id: str,
    index: int,
    org_id: str | None = None,
) -> TrialResponse:
    """Get trial response by 0-based index with optional org scoping."""
    trial_id = f"{task_id}-{index}"
    result = await session.execute(
        select(TrialModel, TaskModel.task_path, TaskModel.org_id)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(TrialModel.id == trial_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    trial, task_path, task_org_id = row
    if org_id is not None and task_org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    queue_info_by_trial_id = await fetch_trial_queue_info(session, trials=[trial])
    jobs_by_subject = await fetch_visible_worker_jobs(session, trial_ids=[trial.id])
    return build_trial_response(
        trial,
        task_path,
        queue_info=queue_info_by_trial_id.get(trial.id),
        jobs=jobs_by_subject.get(("trials", trial.id), []),
    )


async def get_trial_response_for_org_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> TrialResponse:
    """Full TrialResponse for one trial by id (org-scoped via its task).

    Powers ``GET /trials/{trial_id}`` -- the on-click full-detail fetch for the
    experiment grid, which loads only slim trials up front. Same builder as
    ``get_trial_by_index_core`` but keyed on the trial id directly.
    """
    result = await session.execute(
        select(TrialModel, TaskModel.task_path, TaskModel.org_id)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(TrialModel.id == trial_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    trial, task_path, task_org_id = row
    if org_id is not None and task_org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    queue_info_by_trial_id = await fetch_trial_queue_info(session, trials=[trial])
    jobs_by_subject = await fetch_visible_worker_jobs(session, trial_ids=[trial.id])
    return build_trial_response(
        trial,
        task_path,
        queue_info=queue_info_by_trial_id.get(trial.id),
        jobs=jobs_by_subject.get(("trials", trial.id), []),
    )


async def retry_trial_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict[str, str]:
    """Spawn a fresh immutable trial that replaces ``trial_id``.

    Trials are append-only. A retry never resets the existing row;
    instead it inserts a new trial that copies the spec, marks the
    old row as superseded (so it disappears from default UI views and
    no longer counts toward verdict / pipeline aggregation), and
    enqueues a worker_job for the new trial.

    Each trial therefore owns a unique S3 prefix
    (``tasks/{task_id}/trials/{trial_id}/``), which keeps the file
    viewer free of stale folders left over from previous attempts.
    """
    old_trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    task = await session.get(TaskModel, old_trial.task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    if old_trial.superseded_by_trial_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "This trial has already been superseded by another retry "
                f"({old_trial.superseded_by_trial_id}); retry that one instead"
            ),
        )

    # Allow retrying terminal states OR stuck trials.
    # A trial is "stuck" if running/retrying with error or completed harbor stage.
    terminal_states = {TrialStatus.FAILED, TrialStatus.SUCCESS}
    is_stuck = old_trial.status in {
        TrialStatus.RUNNING,
        TrialStatus.RETRYING,
    } and (old_trial.error_message or old_trial.harbor_stage == "completed")
    if old_trial.status not in terminal_states and not is_stuck:
        raise HTTPException(
            status_code=400,
            detail=(
                "Can only retry completed, failed, or stuck trials "
                f"(current: {old_trial.status.value})"
            ),
        )

    # Imported lazily to avoid a circular import through
    # ``oddish.queue`` -> ``oddish.workers.jobs.enqueue``.
    from oddish.queue import enqueue_trial_worker_job, reserve_next_trial_index

    next_index = await reserve_next_trial_index(session, task_id=task.id)
    new_trial_id = f"{task.id}-{next_index}"
    new_trial_name = f"{task.name}-{next_index}"

    new_trial = TrialModel(
        id=new_trial_id,
        name=new_trial_name,
        task_id=old_trial.task_id,
        task_version_id=old_trial.task_version_id,
        experiment_id=old_trial.experiment_id,
        org_id=old_trial.org_id,
        agent=old_trial.agent,
        provider=old_trial.provider,
        queue_key=old_trial.queue_key,
        model=old_trial.model,
        timeout_minutes=old_trial.timeout_minutes,
        environment=old_trial.environment,
        harbor_config=old_trial.harbor_config,
        is_probe=old_trial.is_probe,
        max_attempts=old_trial.max_attempts,
        status=TrialStatus.QUEUED,
    )
    session.add(new_trial)
    # ``superseded_by_trial_id`` is a self-referential FK. Flush the new
    # row before pointing the old row at it so Postgres never sees an
    # UPDATE that references a trial id that has not been inserted yet.
    await session.flush()

    # Mark the old row superseded so it stops showing up in the trial
    # viewer, file viewer, and verdict / analysis aggregation. We also
    # snap any non-terminal status to a terminal one so legacy queries
    # that don't yet filter on ``superseded_by_trial_id`` (e.g. older
    # dashboards, cleanup safety nets) don't mistake the dead row for
    # active pending work.
    old_trial.superseded_by_trial_id = new_trial_id
    if old_trial.status not in terminal_states:
        old_trial.status = TrialStatus.FAILED
        old_trial.error_message = old_trial.error_message or "Superseded by user retry"
        old_trial.finished_at = old_trial.finished_at or utcnow()
        old_trial.current_worker_id = None
        old_trial.current_queue_slot = None

    # Cancel every live worker_jobs row anchored to the OLD trial id
    # (TRIAL run + any in-flight ANALYSIS) so workers stop heart-beating
    # against a superseded row and release their queue_slot lease
    # before we enqueue work for the new trial.
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = 'Superseded by user retry',
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL
            WHERE  subject_table = 'trials'
              AND  subject_id = :trial_id
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"trial_id": trial_id},
    )

    # The task's cached verdict and any in-flight VERDICT row are
    # computed across the trial set; superseding a member invalidates
    # them. Cancel + reset so the verdict stage can re-run cleanly once
    # the replacement trial's analysis completes.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        task.status = TaskStatus.RUNNING
        task.finished_at = None
    _reset_task_verdict(task)
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = 'Superseded by user retry',
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL
            WHERE  kind::text = 'VERDICT'
              AND  subject_table = 'tasks'
              AND  subject_id = :task_id
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"task_id": task.id},
    )

    await enqueue_trial_worker_job(
        session,
        trial_id=new_trial_id,
        queue_key=new_trial.queue_key,
        org_id=new_trial.org_id,
        max_attempts=new_trial.max_attempts,
        harbor_variant_id=(new_trial.harbor_config or {}).get("variant_id")
        or "default",
    )

    await session.commit()
    return {
        "status": "queued",
        "trial_id": new_trial_id,
        "superseded_trial_id": trial_id,
    }


async def get_trial_logs_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Get trial logs with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_logs(trial)


async def get_trial_logs_structured_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Get structured trial logs with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_logs_structured(trial)


async def get_trial_trajectory_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict | None:
    """Get trial trajectory with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_trajectory(trial)


async def get_trial_result_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Get trial result with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_result(trial)
