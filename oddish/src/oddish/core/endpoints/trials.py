from __future__ import annotations

from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints._common import (
    get_trial_for_org_core,
    _reset_task_verdict,
)
from oddish.core.endpoints.qa_cost import get_trial_qa_costs
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
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    utcnow,
)
from oddish.registry_auth import RegistryCredential, encrypt_credentials
from oddish.schemas import RegistryAuth, TrialResponse


async def _attach_pre_trial_audit(
    session: AsyncSession, response: TrialResponse, task_version_id: str | None
) -> TrialResponse:
    """Attach the pre-trial audit of the version this trial actually ran on.

    Pinned to ``trials.task_version_id``, never the task's current version: a
    task re-uploaded after the audit has findings describing a snapshot this
    trial never saw, and showing those here would misattribute them.

    Single-trial detail paths only. The grid's slim payload carries hundreds of
    trials, and findings on each would balloon it.
    """
    if not task_version_id:
        return response
    version = await session.get(TaskVersionModel, task_version_id)
    if version is None:
        return response
    response.pre_trial_findings = (version.pre_trial or {}).get("items") or []
    response.pre_trial_status = (
        version.pre_trial_status.value if version.pre_trial_status else None
    )
    response.pre_trial_error = version.pre_trial_error
    response.pre_trial_cost_usd = (version.pre_trial or {}).get("cost_usd")
    return response


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
    qa_costs = await get_trial_qa_costs(session, trial_ids=[trial.id], org_id=org_id)
    response = build_trial_response(
        trial,
        task_path,
        queue_info=queue_info_by_trial_id.get(trial.id),
        jobs=jobs_by_subject.get(("trials", trial.id), []),
        qa_cost_usd=qa_costs.get(trial.id),
    )
    return await _attach_pre_trial_audit(session, response, trial.task_version_id)


# Mirrors ANALYSIS_CLAIM_TTL_MINUTES in workers/queue/analysis_handler.py.
# Copied, not imported: importing the worker module here would pull the
# whole analyzer stack into the API process.
_ANALYSIS_CLAIM_TTL_MINUTES = 35


async def rerun_trial_analysis_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Queue analysis for one trial.

    This is the independent trial-level trigger. It resets and re-runs
    only this trial's analysis. It does not touch other trials, the task
    verdict, or the pre-trial audit. It is blocked only while this
    trial's own analysis is active.
    """
    from datetime import timedelta

    result = await session.execute(
        select(TrialModel, TaskModel.org_id)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(TrialModel.id == trial_id)
        .with_for_update(of=TrialModel)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
    trial, task_org_id = row
    if org_id is not None and task_org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    if trial.status not in (TrialStatus.SUCCESS, TrialStatus.FAILED):
        raise HTTPException(
            status_code=400,
            detail="The trial must finish before analysis can run",
        )
    if trial.analysis_status in (AnalysisStatus.PENDING, AnalysisStatus.QUEUED):
        raise HTTPException(
            status_code=400,
            detail="Analysis is already queued for this trial",
        )
    if trial.analysis_status == AnalysisStatus.RUNNING:
        started = trial.analysis_started_at
        # A RUNNING claim inside its TTL belongs to a live worker. Past the
        # TTL the worker is presumed dead and a re-run is the remedy.
        if started is not None and utcnow() - started < timedelta(
            minutes=_ANALYSIS_CLAIM_TTL_MINUTES
        ):
            raise HTTPException(
                status_code=400,
                detail="Analysis is already running for this trial",
            )

    # A failed analysis can leave its job in RETRYING. Enqueuing another job
    # then would run the analysis twice. The existing job serves the re-run.
    active_job = await session.scalar(
        select(WorkerJobModel.id).where(
            WorkerJobModel.kind == WorkerJobKind.ANALYSIS,
            WorkerJobModel.subject_table == "trials",
            WorkerJobModel.subject_id == trial_id,
            WorkerJobModel.status.in_(
                [
                    WorkerJobStatus.QUEUED,
                    WorkerJobStatus.RETRYING,
                    WorkerJobStatus.RUNNING,
                ]
            ),
        )
        .limit(1)
    )
    if active_job is not None:
        raise HTTPException(
            status_code=400,
            detail="An analysis job is already queued or running for this trial",
        )

    from oddish.core.endpoints.qa import _reset_trial_analysis
    from oddish.queue import enqueue_trial_analysis_worker_job

    _reset_trial_analysis(trial)
    trial.analysis_status = AnalysisStatus.QUEUED
    await enqueue_trial_analysis_worker_job(
        session, trial_id=trial.id, org_id=task_org_id
    )
    await session.commit()
    return {"status": "queued", "trial_id": trial_id}


async def get_trial_analysis_log_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """The log of the trial's current/most recent analysis run.

    Served on its own endpoint (not on TrialResponse) so trial lists never
    carry it. ``queue_position`` is the 1-based position of the waiting
    analysis job in the QA queue, else None.
    """
    result = await session.execute(
        select(
            TrialModel.analysis_log,
            TrialModel.task_id,
            TaskModel.org_id,
        )
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(TrialModel.id == trial_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
    analysis_log, task_id, task_org_id = row
    if org_id is not None and task_org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
    return {
        "log": analysis_log,
        "queue_position": await _qa_queue_position(
            session, task_id=task_id, trial_id=trial_id
        ),
    }


async def _qa_queue_position(
    session: AsyncSession, *, task_id: str, trial_id: str
) -> int | None:
    """1-based position of the waiting analysis job in the QA queue.

    Two job shapes can carry this trial's analysis: a trial-level ANALYSIS
    job (subject: the trial), or the task-level QA job (subject: the task).
    The trial-level job wins when both wait. Ordering matches the worker's
    claim query for non-trial kinds: ``priority DESC, created_at ASC``
    within the job's queue_key. None when no job is waiting (already
    running, finished, or never enqueued).
    """
    waiting = [WorkerJobStatus.QUEUED, WorkerJobStatus.RETRYING]

    async def _waiting_job(kind: WorkerJobKind, subject_table: str, subject_id: str):
        return (
            await session.execute(
                select(
                    WorkerJobModel.queue_key,
                    WorkerJobModel.priority,
                    WorkerJobModel.created_at,
                )
                .where(
                    WorkerJobModel.kind == kind,
                    WorkerJobModel.subject_table == subject_table,
                    WorkerJobModel.subject_id == subject_id,
                    WorkerJobModel.status.in_(waiting),
                )
                .order_by(WorkerJobModel.created_at.asc())
                .limit(1)
            )
        ).first()

    job = await _waiting_job(WorkerJobKind.ANALYSIS, "trials", trial_id)
    if job is None:
        job = await _waiting_job(WorkerJobKind.QA, "tasks", task_id)
    if job is None:
        return None
    queue_key, priority, created_at = job
    ahead = await session.scalar(
        select(func.count(WorkerJobModel.id)).where(
            WorkerJobModel.queue_key == queue_key,
            WorkerJobModel.status.in_(waiting),
            or_(
                WorkerJobModel.priority > priority,
                and_(
                    WorkerJobModel.priority == priority,
                    WorkerJobModel.created_at < created_at,
                ),
            ),
        )
    )
    return int(ahead or 0) + 1


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
    qa_costs = await get_trial_qa_costs(session, trial_ids=[trial.id], org_id=org_id)
    response = build_trial_response(
        trial,
        task_path,
        queue_info=queue_info_by_trial_id.get(trial.id),
        jobs=jobs_by_subject.get(("trials", trial.id), []),
        qa_cost_usd=qa_costs.get(trial.id),
    )
    return await _attach_pre_trial_audit(session, response, trial.task_version_id)


async def retry_trial_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
    registry_auth: list[RegistryAuth] | None = None,
    gate_baselines: bool = True,
) -> dict:
    """Create a new trial that replaces an old one.

    POST-COMMIT CONTRACT: the superseded run's remote handles are RETURNED as
    ``modal_function_call_ids`` + ``worker_targets``, never terminated here.
    The caller must run ``oddish.core.helpers.terminate_run_harvest(result)``
    after commit or the containers leak until provider TTL (the worker's
    superseded-branch harvest and the reaper remain backstops).

    ``gate_baselines`` is a fresh, per-retry decision (the opt-out does not
    persist from the original run): when True (default) a retried LLM trial
    re-consults its scope's baselines; ``--no-baseline-gate`` sets it False to
    re-run the trial ungated.
    """
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

    terminal_states = {
        TrialStatus.FAILED,
        TrialStatus.SUCCESS,
        TrialStatus.SKIPPED,
    }
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

    from oddish.config import is_nop_oracle_agent, settings
    from oddish.queue import (
        apply_baseline_gate_to_new_llm_trials,
        enqueue_trial_worker_job,
        reserve_next_trial_index,
    )

    from oddish.core.quota_admission import admit_trials

    await admit_trials(
        session,
        org_id,
        old_trial.billed_user_id,
        count=1,
        allow_unattributed=True,
    )

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
        billed_user_id=old_trial.billed_user_id,
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

    # A collection gathers a trial through ``experiment_trials`` without
    # rewriting its scalar ``experiment_id``, so copying that column alone
    # leaves the retry invisible there: the old trial drops out as superseded
    # and the new one was never gathered. Inherit the live membership rows.
    await session.execute(
        text(
            """
            INSERT INTO experiment_trials (experiment_id, trial_id, created_at)
            SELECT experiment_id, :new_trial_id, NOW()
            FROM   experiment_trials
            WHERE  trial_id = :old_trial_id
              AND  deleted_at IS NULL
            """
        ),
        {"new_trial_id": new_trial_id, "old_trial_id": old_trial.id},
    )

    cas = await session.execute(
        text(
            """
            UPDATE trials
            SET    superseded_by_trial_id = :new_trial_id,
                   status = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS', 'SKIPPED')
                       THEN 'FAILED'::jobstatus ELSE status END,
                   error_message = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS', 'SKIPPED')
                       THEN COALESCE(error_message, 'Superseded by user retry')
                       ELSE error_message END,
                   finished_at = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS', 'SKIPPED')
                       THEN COALESCE(finished_at, NOW()) ELSE finished_at END,
                   current_worker_id = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS', 'SKIPPED')
                       THEN NULL ELSE current_worker_id END,
                   current_queue_slot = CASE
                       WHEN status::text NOT IN ('FAILED', 'SUCCESS', 'SKIPPED')
                       THEN NULL ELSE current_queue_slot END
            WHERE  id = :old_trial_id
              AND  superseded_by_trial_id IS NULL
              AND  deleted_at IS NULL
            """
        ),
        {
            "new_trial_id": new_trial_id,
            "old_trial_id": old_trial.id,
        },
    )
    if cast(Any, cas).rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "This trial was just superseded by another retry or deleted; "
                "not re-queued"
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

    cancelled_jobs = await session.execute(
        text(
            """
            WITH to_cancel AS (
                SELECT id, modal_function_call_id, provider, external_id
                FROM   worker_jobs
                WHERE  subject_table = 'trials'
                  AND  subject_id = :trial_id
                  AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                ORDER BY id
                FOR UPDATE
            )
            UPDATE worker_jobs w
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = 'Superseded by user retry',
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL,
                   payload = payload - 'registry_auth_enc'
            FROM   to_cancel
            WHERE  w.id = to_cancel.id
            RETURNING to_cancel.modal_function_call_id,
                      to_cancel.provider,
                      to_cancel.external_id
            """
        ),
        {"trial_id": trial_id},
    )
    modal_fc_ids: list[str] = []
    worker_targets: set[tuple[str, str]] = set()
    for fc, provider, external_id in cancelled_jobs:
        if fc:
            modal_fc_ids.append(str(fc))
        if provider and external_id:
            worker_targets.add((str(provider), str(external_id)))

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

    # Task deleted while we worked (its tombstone won the row-lock interleaving
    # the old-trial CAS guard doesn't cover): the new trial would be
    # unclaimable yet counted inflight forever. Bail before enqueue.
    task_deleted_at = await session.scalar(
        text("SELECT deleted_at FROM tasks WHERE id = :task_id"),
        {"task_id": task.id},
    )
    if task_deleted_at is not None:
        raise HTTPException(
            status_code=409,
            detail="This trial's task was just deleted; not re-queued",
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

    # Retrying an LLM trial re-arms the gate: a retried agent trial consults its
    # scope's baselines just like a fresh one, so it can't bypass a faulty task.
    # The gate may leave it BLOCKED (baselines still running) or CANCELLED
    # (faulty baselines), so report its real state rather than always "queued".
    status_label = "queued"
    if (
        settings.gate_llm_on_baselines
        and gate_baselines
        and not is_nop_oracle_agent(new_trial.agent)
    ):
        await apply_baseline_gate_to_new_llm_trials(
            session,
            task_id=new_trial.task_id,
            task_version_id=new_trial.task_version_id,
            experiment_id=new_trial.experiment_id,
            llm_trial_ids=[new_trial_id],
        )
        final_status = await session.scalar(
            select(WorkerJobModel.status).where(
                WorkerJobModel.subject_table == "trials",
                WorkerJobModel.kind == WorkerJobKind.TRIAL,
                WorkerJobModel.subject_id == new_trial_id,
            )
        )
        status_label = {
            WorkerJobStatus.QUEUED: "queued",
            WorkerJobStatus.BLOCKED: "blocked",
            WorkerJobStatus.CANCELLED: "skipped",
        }.get(final_status, "queued")

    await session.commit()
    return {
        "status": status_label,
        "trial_id": new_trial_id,
        "superseded_trial_id": trial_id,
        "modal_function_call_ids": modal_fc_ids,
        "worker_targets": sorted(worker_targets),
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
