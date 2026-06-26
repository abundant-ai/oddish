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
)
from oddish.registry_auth import RegistryCredential, encrypt_credentials
from oddish.schemas import RegistryAuth, TrialResponse


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


async def retry_trial_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
    registry_auth: list[RegistryAuth] | None = None,
) -> dict[str, str]:
    """Create a new trial that replaces an old one."""
    old_trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    old_trial = await session.get(TrialModel, old_trial.id)
    if old_trial is None:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    if old_trial.superseded_by_trial_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "This trial has already been superseded by another retry "
                f"({old_trial.superseded_by_trial_id}); retry that one instead"
            ),
        )

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

    task = await session.get(TaskModel, old_trial.task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

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
    await session.flush()

    cas = await session.execute(
        text(
            """
            UPDATE trials
            SET    superseded_by_trial_id = :new_trial_id,
                   status = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS')
                       THEN 'FAILED'::jobstatus ELSE status END,
                   error_message = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS')
                       THEN COALESCE(error_message, 'Superseded by user retry')
                       ELSE error_message END,
                   finished_at = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS')
                       THEN COALESCE(finished_at, NOW()) ELSE finished_at END,
                   current_worker_id = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS')
                       THEN NULL ELSE current_worker_id END,
                   current_queue_slot = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS')
                       THEN NULL ELSE current_queue_slot END
            WHERE  id = :old_trial_id
              AND  superseded_by_trial_id IS NULL
            """
        ),
        {"new_trial_id": new_trial_id, "old_trial_id": old_trial.id},
    )
    if cas.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "This trial was just superseded by another retry; "
                "retry the new trial instead"
            ),
        )
    session.expire(old_trial)

    if registry_auth:
        registry_auth_enc = encrypt_credentials(
            [
                RegistryCredential(
                    username=auth.username,
                    token=auth.token.get_secret_value(),
                    registry=auth.registry,
                )
                for auth in registry_auth
            ]
        )
    else:
        registry_auth_enc = await session.scalar(
            text(
                """
                SELECT payload->>'registry_auth_enc'
                FROM   worker_jobs
                WHERE  kind::text = 'TRIAL'
                  AND  subject_table = 'trials'
                  AND  subject_id = :trial_id
                ORDER BY created_at DESC
                LIMIT  1
                """
            ),
            {"trial_id": trial_id},
        )

    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = 'Superseded by user retry',
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL,
                   payload = payload - 'registry_auth_enc'
            WHERE  subject_table = 'trials'
              AND  subject_id = :trial_id
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"trial_id": trial_id},
    )

    if task.status in (
        TaskStatus.ANALYZING,
        TaskStatus.VERDICT_PENDING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    ):
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
            WHERE  kind::text IN ('QA', 'VERDICT')
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
        registry_auth_enc=registry_auth_enc,
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
