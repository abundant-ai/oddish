from __future__ import annotations

from collections.abc import Collection

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.endpoints._common import (
    get_trial_for_org_core,
    _reset_task_verdict,
)
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    utcnow,
)
from oddish.schemas import ExperimentCombineResponse


def _task_has_active_analysis(task: TaskModel) -> bool:
    return any(
        trial.superseded_by_trial_id is None
        and trial.analysis_status
        in (AnalysisStatus.PENDING, AnalysisStatus.QUEUED, AnalysisStatus.RUNNING)
        for trial in task.trials or []
    )


def _task_has_active_trials(task: TaskModel) -> bool:
    return any(
        trial.superseded_by_trial_id is None
        and trial.status
        in (
            TrialStatus.PENDING,
            TrialStatus.QUEUED,
            TrialStatus.RUNNING,
            TrialStatus.RETRYING,
        )
        for trial in task.trials or []
    )


def _clear_stale_task_pipeline_status(task: TaskModel) -> None:
    """Move a surviving task out of pipeline-only states after scoped deletion."""
    if task.status not in (TaskStatus.ANALYZING, TaskStatus.VERDICT_PENDING):
        return
    if _task_has_active_trials(task) or _task_has_active_analysis(task):
        return
    if task.verdict_status in (
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    ):
        return

    live_trials = [
        trial for trial in task.trials or [] if trial.superseded_by_trial_id is None
    ]
    task.status = TaskStatus.COMPLETED if live_trials else TaskStatus.FAILED
    task.finished_at = task.finished_at or utcnow()


async def delete_task_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
    experiment_id: str | None = None,
) -> dict:
    """Soft-delete a task and its trials, optionally scoped to one experiment.

    "Delete" here is a tombstone: rows are kept in the database with
    ``deleted_at`` stamped to ``NOW()``, the session-level filter in
    :mod:`oddish.db.soft_delete` hides them from normal reads, and S3
    artifacts are *not* removed (the response carries an empty
    ``s3_prefixes`` list so callers' best-effort S3 cleanup is a no-op).

    When ``experiment_id`` is ``None`` the task and every one of its
    trials are tombstoned. ``task_experiments`` link rows are left intact
    so a future restore can recover the experiment membership.

    When ``experiment_id`` is given, only trials whose ``experiment_id``
    matches are tombstoned and the ``(task_id, experiment_id)`` join row
    is tombstoned. The task itself is only tombstoned if no live trials
    and no other live experiment links remain.
    """
    task_query = select(
        TaskModel.id,
        TaskModel.task_s3_key,
        TaskModel.task_path,
    ).where(TaskModel.id == task_id)
    if org_id is not None:
        task_query = task_query.where(TaskModel.org_id == org_id)
    task_result = await session.execute(task_query)
    task_row = task_result.one_or_none()
    if not task_row:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    resolved_task_id, _task_s3_key, _task_path = task_row

    from oddish.db import task_experiments

    # Unscoped: tombstone the task and all its trials.
    if experiment_id is None:
        scoped_trial_ids = [
            row[0]
            for row in (
                await session.execute(
                    select(TrialModel.id).where(TrialModel.task_id == resolved_task_id)
                )
            ).all()
        ]

        if scoped_trial_ids:
            await _cancel_worker_jobs_for_trials(
                session,
                trial_ids=scoped_trial_ids,
                reason="Task deleted by user",
            )

        await _cancel_worker_jobs_for_task(
            session,
            task_id=resolved_task_id,
            reason="Task deleted by user",
        )

        await session.execute(
            update(TrialModel)
            .where(TrialModel.task_id == resolved_task_id)
            .values(deleted_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == resolved_task_id)
            .values(deleted_at=utcnow())
            .execution_options(synchronize_session=False)
        )

        return {
            # Soft-delete preserves S3 artifacts for potential restore.
            # The response key is kept so the API contract with callers
            # (e.g. backend's ``delete_s3_prefixes`` best-effort cleanup)
            # remains stable; the empty list makes that cleanup a no-op.
            "s3_prefixes": [],
            "deleted": {"task_id": task_id},
        }

    # Scoped delete: only this experiment's trials + the join row.
    scoped_trial_rows = (
        await session.execute(
            select(TrialModel.id, TrialModel.trial_s3_key).where(
                TrialModel.task_id == resolved_task_id,
                TrialModel.experiment_id == experiment_id,
            )
        )
    ).all()
    scoped_trial_ids = [row[0] for row in scoped_trial_rows]

    # Check that this task really belongs to the given experiment.
    link_exists = await session.scalar(
        select(func.count())
        .select_from(task_experiments)
        .where(
            task_experiments.c.task_id == resolved_task_id,
            task_experiments.c.experiment_id == experiment_id,
            task_experiments.c.deleted_at.is_(None),
        )
    )
    if not link_exists and not scoped_trial_ids:
        raise HTTPException(
            status_code=404,
            detail=(f"Task {task_id} has no trials in experiment {experiment_id}"),
        )

    # Cancel live worker_jobs for those trials so workers release slots.
    if scoped_trial_ids:
        await _cancel_worker_jobs_for_trials(
            session,
            trial_ids=scoped_trial_ids,
            reason="Task deleted by user",
        )

        await session.execute(
            update(TrialModel)
            .where(TrialModel.id.in_(scoped_trial_ids))
            .values(deleted_at=utcnow())
            .execution_options(synchronize_session=False)
        )

    # Tombstone the join row so live views stop listing this membership
    # without losing restore/audit history.
    await session.execute(
        update(task_experiments)
        .where(
            task_experiments.c.task_id == resolved_task_id,
            task_experiments.c.experiment_id == experiment_id,
            task_experiments.c.deleted_at.is_(None),
        )
        .values(deleted_at=utcnow())
    )

    # If the task has no remaining live trials and no other experiment
    # links, tombstone it too. Live = ``deleted_at IS NULL`` (the
    # session-level filter handles this transparently for ORM queries).
    remaining_trials = int(
        await session.scalar(
            select(func.count(TrialModel.id)).where(
                TrialModel.task_id == resolved_task_id
            )
        )
        or 0
    )
    remaining_links = int(
        await session.scalar(
            select(func.count())
            .select_from(task_experiments)
            .where(
                task_experiments.c.task_id == resolved_task_id,
                task_experiments.c.deleted_at.is_(None),
            )
        )
        or 0
    )

    task_removed = False
    if remaining_trials == 0 and remaining_links == 0:
        await _cancel_worker_jobs_for_task(
            session,
            task_id=resolved_task_id,
            reason="Task deleted by user",
        )
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == resolved_task_id)
            .values(deleted_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        task_removed = True
    else:
        task = await session.get(TaskModel, resolved_task_id)
        if task is not None:
            _reset_task_verdict(task)

    return {
        "s3_prefixes": [],
        "deleted": {
            "task_id": task_id,
            "experiment_id": experiment_id,
            "trials_deleted": len(scoped_trial_ids),
            "task_removed": task_removed,
        },
    }


async def _cancel_worker_jobs_for_trials(
    session: AsyncSession,
    *,
    trial_ids: Collection[str],
    reason: str,
) -> None:
    """Cancel live worker_jobs whose subject is one of these trials.

    Soft-deleting a trial doesn't kill its in-flight scheduling state.
    Without this cancel the dispatcher could still claim the row (the
    claim SQL guards against soft-deleted subjects defensively, but the
    canonical signal is ``worker_jobs.status``) and a heart-beating
    worker would keep holding a queue slot. Issued as raw SQL because
    ``worker_jobs.status`` is a Postgres enum and the cast keeps the
    statement portable across asyncpg / SQLAlchemy boundaries.
    """
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = :reason,
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL,
                   payload = payload - 'registry_auth_enc'
            WHERE  subject_table = 'trials'
              AND  subject_id = ANY(:trial_ids)
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"trial_ids": list(trial_ids), "reason": reason},
    )


async def _cancel_worker_jobs_for_task(
    session: AsyncSession,
    *,
    task_id: str,
    reason: str,
) -> None:
    """Cancel live worker_jobs whose subject is this task (e.g. VERDICT)."""
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = :reason,
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL
            WHERE  subject_table = 'tasks'
              AND  subject_id = :task_id
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"task_id": task_id, "reason": reason},
    )


async def delete_experiment_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> dict:
    """Soft-delete an experiment, its trials, and any now-orphaned tasks.

    Like :func:`delete_task_core` this is tombstone-based: rows get
    ``deleted_at`` stamped instead of being physically removed, and S3
    artifacts are preserved. ``task_experiments`` link rows that pointed
    at this experiment are tombstoned so list views immediately stop
    surfacing the membership while keeping enough history for restore.
    """
    from oddish.db import task_experiments

    exp_query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id is not None:
        exp_query = exp_query.where(ExperimentModel.org_id == org_id)

    exp_result = await session.execute(exp_query)
    experiment = exp_result.scalar_one_or_none()
    if not experiment:
        raise HTTPException(
            status_code=404, detail=f"Experiment {experiment_id} not found"
        )

    # Tasks linked to this experiment -- snapshot them now so we can check
    # which ones orphan out after the scoped trial tombstone + link drop.
    linked_task_rows = (
        await session.execute(
            select(TaskModel.id)
            .join(
                task_experiments,
                task_experiments.c.task_id == TaskModel.id,
            )
            .where(task_experiments.c.experiment_id == experiment_id)
            .where(task_experiments.c.deleted_at.is_(None))
        )
    ).all()
    linked_task_ids = [row[0] for row in linked_task_rows]

    if linked_task_ids:
        for tid in linked_task_ids:
            await _cancel_worker_jobs_for_task(
                session,
                task_id=tid,
                reason="Experiment deleted by user",
            )

    # Trials scoped to this experiment.
    trial_where = [TrialModel.experiment_id == experiment_id]
    if org_id is not None:
        trial_where.append(
            or_(TrialModel.org_id == org_id, TrialModel.org_id.is_(None))
        )

    scoped_trial_ids = [
        row[0]
        for row in (
            await session.execute(select(TrialModel.id).where(*trial_where))
        ).all()
    ]

    if scoped_trial_ids:
        await _cancel_worker_jobs_for_trials(
            session,
            trial_ids=scoped_trial_ids,
            reason="Experiment deleted by user",
        )

        trials_upd = await session.execute(
            update(TrialModel)
            .where(TrialModel.id.in_(scoped_trial_ids))
            .values(deleted_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        deleted_trials = int(trials_upd.rowcount or 0)  # type: ignore[attr-defined]
    else:
        deleted_trials = 0

    # Tombstone experiment->task link rows so list views stop pulling
    # this experiment into the task's membership.
    await session.execute(
        update(task_experiments)
        .where(
            task_experiments.c.experiment_id == experiment_id,
            task_experiments.c.deleted_at.is_(None),
        )
        .values(deleted_at=utcnow())
    )

    # Tombstone the experiment row.
    experiments_upd_query = (
        update(ExperimentModel)
        .where(ExperimentModel.id == experiment_id)
        .values(deleted_at=utcnow())
    )
    if org_id is not None:
        experiments_upd_query = experiments_upd_query.where(
            ExperimentModel.org_id == org_id
        )
    experiments_result = await session.execute(experiments_upd_query)
    deleted_experiments = int(experiments_result.rowcount or 0)  # type: ignore[attr-defined]

    # Any of the previously-linked tasks that now have no live trials
    # and no other experiment links are orphaned -- tombstone them too.
    deleted_tasks = 0

    for tid in linked_task_ids:
        remaining_trials = int(
            await session.scalar(
                select(func.count(TrialModel.id)).where(TrialModel.task_id == tid)
            )
            or 0
        )
        remaining_links = int(
            await session.scalar(
                select(func.count())
                .select_from(task_experiments)
                .where(
                    task_experiments.c.task_id == tid,
                    task_experiments.c.deleted_at.is_(None),
                )
            )
            or 0
        )
        if remaining_trials == 0 and remaining_links == 0:
            await _cancel_worker_jobs_for_task(
                session,
                task_id=tid,
                reason="Experiment deleted by user",
            )
            task_upd_result = await session.execute(
                update(TaskModel)
                .where(TaskModel.id == tid)
                .values(deleted_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            deleted_tasks += int(task_upd_result.rowcount or 0)  # type: ignore[attr-defined]
        else:
            task_result = await session.execute(
                select(TaskModel)
                .options(selectinload(TaskModel.trials))
                .where(TaskModel.id == tid)
            )
            task = task_result.scalar_one_or_none()
            if task is not None:
                _reset_task_verdict(task)
                _clear_stale_task_pipeline_status(task)

    return {
        "s3_prefixes": [],
        "deleted": {
            "trials": deleted_trials,
            "tasks": deleted_tasks,
            "experiments": deleted_experiments,
        },
    }


# Trial columns whose execution/claim/heartbeat state is meaningless on a
# copy. Everything *not* listed here is carried over from the source trial
# so the combined trial keeps its results, analysis, token usage, and timing.
_COMBINE_TRIAL_RESULT_FIELDS = (
    "task_version_id",
    "agent",
    "provider",
    "queue_key",
    "model",
    "timeout_minutes",
    "environment",
    "harbor_config",
    "is_probe",
    "status",
    "origin",
    "attempts",
    "max_attempts",
    "harbor_stage",
    "reward",
    "error_message",
    "result",
    "input_tokens",
    "cache_tokens",
    "output_tokens",
    "total_steps",
    "cost_usd",
    "phase_timing",
    "has_trajectory",
    "analysis",
    "analysis_status",
    "analysis_error",
    "analysis_started_at",
    "analysis_finished_at",
    "started_at",
    "finished_at",
)


async def combine_experiments_core(
    session: AsyncSession,
    *,
    source_experiment_ids: Collection[str],
    name: str | None = None,
    org_id: str | None = None,
    copy_artifacts: bool = True,
) -> ExperimentCombineResponse:
    """Create a new experiment that merges the data of several others.

    The source experiments are read-only here: a fresh result experiment
    is created and the *underlying data* of every source is copied into
    it. Concretely, for each source experiment we

    1. link its tasks into the result experiment (``task_experiments``),
       so the result experiment's task list is the union of the sources;
    2. copy every finished (``SUCCESS`` / ``FAILED``), non-superseded
       trial into the result experiment as a new immutable trial row under
       the *same* task, preserving results / analysis / token usage /
       timing; and
    3. duplicate each copied trial's S3 artifacts into the new trial's
       prefix (``copy_artifacts=True``, the default) so the result
       experiment is fully self-contained, or point the copy at the source
       artifacts in place when ``copy_artifacts=False``.

    Non-terminal source trials (still pending/queued/running) have no
    result to combine and are skipped; the count is surfaced in the
    response. No worker jobs are enqueued -- combined trials land already
    terminal, exactly like imported ones.

    The caller's session context manager is responsible for the commit;
    on any error the artifact copy and row inserts roll back together.
    """
    from oddish.db import task_experiments
    from oddish.db.storage import (
        StorageClient,
        get_storage_client,
        resolve_trial_s3_prefix,
    )
    from oddish.experiment import generate_experiment_name
    from oddish.queue import (
        _link_task_to_experiment,
        bump_experiment_last_activity,
        get_experiment_by_id_or_name,
        reserve_next_trial_index,
    )

    # 1. Resolve + validate the sources (org-scoped). Preserve caller order
    #    while collapsing duplicate ids/names onto the same experiment.
    resolved: list[ExperimentModel] = []
    seen_ids: set[str] = set()
    for identifier in source_experiment_ids:
        if not identifier or not identifier.strip():
            continue
        experiment = await get_experiment_by_id_or_name(session, identifier, org_id)
        if experiment is None:
            raise HTTPException(
                status_code=404,
                detail=f"Experiment {identifier} not found",
            )
        if experiment.id in seen_ids:
            continue
        seen_ids.add(experiment.id)
        resolved.append(experiment)

    if len(resolved) < 2:
        raise HTTPException(
            status_code=400,
            detail="Provide at least two distinct experiments to combine",
        )

    resolved_ids = [experiment.id for experiment in resolved]

    # 2. Create the result experiment.
    result_name = (name or "").strip() or generate_experiment_name()
    result = ExperimentModel(
        name=result_name,
        org_id=org_id,
        last_activity_at=utcnow(),
    )
    session.add(result)
    await session.flush()

    # 3. Union of tasks linked to any source experiment.
    linked_task_rows = await session.execute(
        select(task_experiments.c.task_id)
        .where(task_experiments.c.experiment_id.in_(resolved_ids))
        .where(task_experiments.c.deleted_at.is_(None))
        .distinct()
    )
    linked_task_ids = [row[0] for row in linked_task_rows.all()]

    # 4. Finished, non-superseded trials across every source. Ordered by
    #    task so per-task index allocation stays contiguous and stable.
    source_trials = list(
        (
            await session.execute(
                select(TrialModel)
                .where(TrialModel.experiment_id.in_(resolved_ids))
                .where(TrialModel.superseded_by_trial_id.is_(None))
                .order_by(TrialModel.task_id, TrialModel.created_at)
            )
        )
        .scalars()
        .all()
    )

    # Defensive: make sure tasks that own a copied trial are linked too,
    # even if a ``task_experiments`` row was somehow missing.
    all_task_ids = list(
        dict.fromkeys([*linked_task_ids, *(t.task_id for t in source_trials)])
    )
    for task_id in all_task_ids:
        await _link_task_to_experiment(
            session, task_id=task_id, experiment_id=result.id
        )

    # Task name + org are needed to mint new trial ids/names.
    task_meta: dict[str, tuple[str, str | None]] = {}
    if all_task_ids:
        meta_rows = await session.execute(
            select(TaskModel.id, TaskModel.name, TaskModel.org_id).where(
                TaskModel.id.in_(all_task_ids)
            )
        )
        for task_id, task_name, task_org in meta_rows.all():
            task_meta[task_id] = (task_name, task_org)

    # 5. Copy the trials.
    terminal_states = {TrialStatus.SUCCESS, TrialStatus.FAILED}
    next_index_for_task: dict[str, int] = {}
    copy_plan: list[tuple[str, str]] = []
    trials_copied = 0
    trials_skipped = 0

    for source in source_trials:
        if source.status not in terminal_states:
            trials_skipped += 1
            continue

        task_id = source.task_id
        if task_id not in next_index_for_task:
            next_index_for_task[task_id] = await reserve_next_trial_index(
                session, task_id=task_id
            )
        index = next_index_for_task[task_id]
        next_index_for_task[task_id] = index + 1

        new_trial_id = f"{task_id}-{index}"
        task_name, task_org = task_meta.get(task_id, (task_id, source.org_id))
        new_trial_name = f"{task_name}-{index}"

        source_prefix = resolve_trial_s3_prefix(
            source.id,
            trial_s3_key=source.trial_s3_key,
            trial_result_path=source.harbor_result_path,
        )
        if copy_artifacts:
            destination_prefix = StorageClient._trial_prefix(new_trial_id)
            new_trial_s3_key: str | None = destination_prefix
            new_harbor_result_path: str | None = None
            copy_plan.append((source_prefix, destination_prefix))
        else:
            # Reference the source artifacts in place.
            new_trial_s3_key = source_prefix
            new_harbor_result_path = source.harbor_result_path

        idempotency_key = f"combine:{result.id}:{source.id}"
        if len(idempotency_key) > 64:
            idempotency_key = None

        # The copy belongs to the result experiment, so it inherits that
        # experiment's org. ``org_id`` is None only on the single-tenant
        # OSS path, where we fall back to whatever the source carried.
        new_org_id = org_id if org_id is not None else (source.org_id or task_org)

        new_trial = TrialModel(
            id=new_trial_id,
            name=new_trial_name,
            task_id=task_id,
            experiment_id=result.id,
            org_id=new_org_id,
            idempotency_key=idempotency_key,
            trial_s3_key=new_trial_s3_key,
            harbor_result_path=new_harbor_result_path,
            **{field: getattr(source, field) for field in _COMBINE_TRIAL_RESULT_FIELDS},
        )
        session.add(new_trial)
        trials_copied += 1

    await session.flush()

    # 6. Duplicate artifacts server-side. Surfacing a hard failure rolls
    #    the whole transaction back (no orphaned rows committed).
    artifacts_copied = 0
    if copy_artifacts and copy_plan:
        storage = get_storage_client()
        for source_prefix, destination_prefix in copy_plan:
            try:
                artifacts_copied += await storage.copy_prefix(
                    source_prefix, destination_prefix
                )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to copy trial artifacts: {exc}",
                ) from exc

    await bump_experiment_last_activity(session, experiment_ids=result.id)

    return ExperimentCombineResponse(
        id=result.id,
        name=result.name,
        source_experiment_ids=resolved_ids,
        tasks_linked=len(all_task_ids),
        trials_copied=trials_copied,
        trials_skipped=trials_skipped,
        artifacts_copied=artifacts_copied,
    )


async def delete_trial_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Soft-delete a single trial and cancel its in-flight worker_jobs.

    Sets ``deleted_at`` instead of issuing a SQL DELETE so the row stays
    queryable via ``include_deleted=True`` for audit / restore flows.
    The parent task's cached verdict is invalidated so dashboards stop
    aggregating numbers from the now-hidden trial.
    """
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)

    # Cancel any live worker_jobs belonging to this trial (TRIAL runs and
    # ANALYSIS jobs) so workers stop heart-beating and release slots
    # before the domain row goes invisible under them.
    await _cancel_worker_jobs_for_trials(
        session,
        trial_ids=[trial_id],
        reason="Trial deleted by user",
    )

    task_id = trial.task_id

    await session.execute(
        update(TrialModel)
        .where(TrialModel.id == trial_id)
        .values(deleted_at=utcnow())
        .execution_options(synchronize_session=False)
    )

    # Task aggregates (total/completed/failed) are derived from the
    # remaining trials -- the soft-delete filter excludes this one
    # automatically -- but the cached verdict on the task row may
    # reference it directly. Clear it so the dashboard doesn't show
    # stale data.
    if task_id:
        task = await session.get(TaskModel, task_id)
        if task is not None:
            _reset_task_verdict(task)

    return {
        "s3_prefixes": [],
        "deleted": {"trial_id": trial_id, "task_id": task_id},
    }
