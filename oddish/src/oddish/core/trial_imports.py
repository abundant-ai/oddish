"""Core logic for uploading off-oddish Harbor trial results.

The import flow mirrors the task upload pattern:

1. ``/trials/import/init`` (``initialize_trial_import``)
   - Validates the target task and resolves/creates the experiment.
   - Allocates the next ``{task_id}-{index}`` trial ID under a
     ``SELECT ... FOR UPDATE`` lock on the task row so concurrent
     imports cannot collide.
   - Inserts a ``TrialModel`` row in terminal state (SUCCESS/FAILED)
     with ``origin=IMPORTED``.
   - Returns a presigned PUT URL for the staging archive plus the
     resolved trial metadata.

2. Client PUTs a tarball of the Harbor trial subdir to the URL.

3. ``/trials/import/complete`` (``complete_trial_import``)
   - Downloads the staging archive, extracts individual files into
     the trial's S3 prefix, deletes the staging archive, and returns
     the extraction count.
   - Rolls the parent task's status forward via
     ``maybe_start_analysis_stage`` so heterogeneous experiments (some
     live, some imported) transition cleanly when the last pending
     trial settles.

Imports intentionally skip the ``worker_jobs`` queue for trial
execution: imported rows land already-terminal, so the dispatcher /
cleanup loop has nothing to do for them. When the target task has
``run_analysis`` enabled, ``initialize_trial_import`` does enqueue a
per-trial analysis ``worker_job`` (mirroring the live trial handler)
so the analysis / verdict pipeline rolls forward over a mix of live
and imported rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialOrigin,
    get_session,
    utcnow,
)
from oddish.db.storage import StorageClient, get_storage_client
from oddish.experiment import generate_experiment_name
from oddish.schemas import (
    ImportedTrialSpec,
    TrialImportCompleteResponse,
    TrialImportInitResponse,
)


# =============================================================================
# Helpers
# =============================================================================


async def _get_task_for_org(
    session: AsyncSession, task_id: str, org_id: str | None
) -> TaskModel:
    query = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    task: TaskModel | None = (await session.execute(query)).scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Task {task_id} not found. Upload it first with "
                "`oddish upload` (or POST /tasks/upload/init + complete)."
            ),
        )
    return task


async def _resolve_experiment_for_import(
    session: AsyncSession,
    *,
    experiment_id_or_name: str | None,
    org_id: str | None,
) -> ExperimentModel:
    """Pick or create the experiment to attach imported trials to.

    - When ``experiment_id_or_name`` is given, look up by ID, then by
      name within the caller's org (matching ``get_experiment_by_id_or_name``).
      Create on the fly if neither match so the caller can import into
      a brand-new experiment by name.
    - When None, always create a fresh auto-named experiment. This
      matches the default semantics of ``oddish run`` (each invocation
      gets its own experiment unless the user pins one).
    """
    from oddish.queue import (
        get_experiment_by_id_or_name,
        get_or_create_experiment,
    )

    if experiment_id_or_name:
        existing = await get_experiment_by_id_or_name(
            session, experiment_id_or_name, org_id
        )
        if existing is not None:
            return existing
        # Treat the string as a *name* and create it. This keeps the
        # "pin to a specific experiment" UX symmetrical between imports
        # into new and existing experiments.
        return await get_or_create_experiment(session, experiment_id_or_name, org_id)

    return await get_or_create_experiment(session, generate_experiment_name(), org_id)


def _next_trial_index(existing_trial_ids: list[str], task_id: str) -> int:
    """Return the next integer suffix for ``{task_id}-{N}`` IDs.

    Mirrors the logic in ``oddish.queue._get_next_trial_index`` but
    operates on a flat list of IDs we load with a cheap SELECT.
    """
    prefix = f"{task_id}-"
    max_index = -1
    for trial_id in existing_trial_ids:
        if not trial_id.startswith(prefix):
            continue
        suffix = trial_id[len(prefix) :]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))
    return max_index + 1 if max_index >= 0 else len(existing_trial_ids)


def _parse_datetime(value: datetime | None) -> datetime | None:
    """Normalize datetimes from the client to timezone-aware UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _link_task_to_experiment(
    session: AsyncSession, *, task_id: str, experiment_id: str
) -> None:
    from oddish.db import task_experiments

    await session.execute(
        pg_insert(task_experiments)
        .values(task_id=task_id, experiment_id=experiment_id)
        .on_conflict_do_nothing(index_elements=["task_id", "experiment_id"])
    )


# =============================================================================
# Public API
# =============================================================================


async def initialize_trial_import(
    *,
    task_id: str,
    experiment_id_or_name: str | None,
    trial_spec: ImportedTrialSpec,
    upload_artifacts: bool,
    org_id: str | None = None,
) -> TrialImportInitResponse:
    """Create an imported trial row and return a presigned artifact URL.

    See module docstring for the full flow.

    When the target task has ``run_analysis`` enabled, the imported trial
    gets a per-trial analysis ``worker_job`` enqueued the same way a live
    trial does after it reaches a terminal state, so the analysis /
    verdict pipeline can roll forward over a mix of live and imported
    rows.
    """
    async with get_session() as session:
        task = await _get_task_for_org(session, task_id, org_id)

        experiment = await _resolve_experiment_for_import(
            session,
            experiment_id_or_name=experiment_id_or_name,
            org_id=org_id,
        )

        # Load existing trial IDs for this task under the row lock so
        # concurrent imports serialize on index allocation. Use
        # ``include_deleted=True`` so soft-deleted rows still consume
        # their ``{task_id}-{N}`` index — otherwise the PK would collide
        # when a previous trial at the same suffix was soft-deleted but
        # never tombstoned out of the table.
        trial_id_rows = await session.execute(
            select(TrialModel.id)
            .where(TrialModel.task_id == task.id)
            .execution_options(include_deleted=True)
        )
        existing_ids = [row[0] for row in trial_id_rows.all()]
        next_index = _next_trial_index(existing_ids, task.id)
        trial_id = f"{task.id}-{next_index}"

        # Derive routing metadata the same way the live path does so
        # dashboards/aggregations treat imported trials uniformly.
        agent = trial_spec.agent
        model = settings.normalize_trial_model(agent, trial_spec.model)
        provider = settings.get_provider_for_trial(agent, model)
        queue_key = settings.get_queue_key_for_trial(agent, model)

        # Pin the trial to the task's current version so the UI's
        # "version filter" keeps working for imported rows too.
        task_version_id = task.current_version_id
        if task_version_id is None:
            latest_version = await session.scalar(
                select(func.max(TaskVersionModel.version)).where(
                    TaskVersionModel.task_id == task.id
                )
            )
            if latest_version is not None:
                task_version_id = f"{task.id}-v{latest_version}"

        # Imports already have their artifacts produced -- no queue,
        # no retries, single terminal row.
        now = utcnow()
        started_at = _parse_datetime(trial_spec.started_at) or now
        finished_at = _parse_datetime(trial_spec.finished_at) or now

        # When the client supplies a stable external ID, scope it by
        # the target experiment so an accidental re-import into the
        # *same* experiment collides on the unique index, but the same
        # source trial can still be merged into a *different*
        # experiment as a separate row.
        if trial_spec.external_trial_id:
            idempotency_key = f"import:{trial_spec.external_trial_id}:{experiment.id}"
        else:
            idempotency_key = f"import-{uuid.uuid4()}"

        trial_row = TrialModel(
            id=trial_id,
            name=f"{task.name}-{next_index}",
            task_id=task.id,
            task_version_id=task_version_id,
            experiment_id=experiment.id,
            org_id=task.org_id,
            idempotency_key=idempotency_key,
            agent=agent,
            provider=provider,
            queue_key=queue_key,
            model=model,
            environment=(
                trial_spec.environment.value
                if trial_spec.environment is not None
                else None
            ),
            harbor_config=trial_spec.harbor_config,
            status=trial_spec.status,
            origin=TrialOrigin.IMPORTED,
            attempts=1,
            max_attempts=1,
            harbor_stage=trial_spec.harbor_stage or "completed",
            reward=trial_spec.reward,
            error_message=trial_spec.error_message,
            input_tokens=trial_spec.input_tokens,
            cache_tokens=trial_spec.cache_tokens,
            output_tokens=trial_spec.output_tokens,
            cost_usd=trial_spec.cost_usd,
            phase_timing=trial_spec.phase_timing,
            has_trajectory=trial_spec.has_trajectory,
            trial_s3_key=StorageClient._trial_prefix(trial_id),
            started_at=started_at,
            finished_at=finished_at,
        )
        session.add(trial_row)

        # Keep the task ↔ experiment association in sync. Imports can
        # attach a task that already belongs to other experiments to an
        # additional one via ``--experiment``.
        await _link_task_to_experiment(
            session, task_id=task.id, experiment_id=experiment.id
        )

        # Roll forward the task status now that we've added a terminal
        # trial. Mirror the logic in ``append_trials_to_task``:
        #
        # - PENDING / RUNNING: stay as-is; stage transition below will
        #   flip to COMPLETED / ANALYZING once every trial is terminal.
        # - COMPLETED / FAILED: reset to RUNNING and clear
        #   ``finished_at`` so the stage transition can flip it back
        #   with an updated timestamp that reflects the newly-imported
        #   trials.
        # - ANALYZING / VERDICT_PENDING: an import on a run_analysis
        #   task can land while the task is mid-analysis; bounce it
        #   back to RUNNING so the stage transition re-evaluates with
        #   the new trial included.
        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.RUNNING
            task.started_at = task.started_at or started_at
        elif task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.ANALYZING,
            TaskStatus.VERDICT_PENDING,
        ):
            task.status = TaskStatus.RUNNING
            task.finished_at = None

        await session.flush()

        # Mirror the per-trial analysis enqueue that the live trial
        # handler runs when a trial reaches a terminal state, so
        # imported rows participate in the analysis / verdict pipeline
        # exactly like live ones.
        from oddish.queue import (
            enqueue_analysis_worker_job,
            maybe_start_analysis_stage,
        )

        if task.run_analysis and trial_row.analysis_status is None:
            trial_row.analysis_status = AnalysisStatus.QUEUED
            await enqueue_analysis_worker_job(
                session, trial_id=trial_id, org_id=task.org_id
            )

        # Run the stage-transition here too so tasks whose only trials
        # are imported (and especially the ``--skip-artifacts`` path,
        # where ``complete`` is never called) still transition to
        # COMPLETED. ``maybe_start_analysis_stage`` is idempotent --
        # calling it again in ``complete_trial_import`` is a no-op.
        await maybe_start_analysis_stage(session, trial_id)

        await session.commit()

    # Build the presign response *after* commit so the row is durable
    # before the client starts uploading artifacts.
    trial_s3_key = StorageClient._trial_prefix(trial_id)
    archive_s3_key: str | None = None
    upload_url: str | None = None
    upload_headers: dict[str, str] = {}
    upload_method: str | None = None
    requires_completion = False

    if upload_artifacts:
        storage = get_storage_client()
        archive_s3_key = StorageClient._trial_import_archive_key(trial_id)
        try:
            upload_url = await storage.get_presigned_upload_url(
                archive_s3_key,
                expiration=3600,
                content_type="application/gzip",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to prepare S3 upload: {str(exc)}",
            ) from exc
        upload_method = "PUT"
        upload_headers = {"Content-Type": "application/gzip"}
        requires_completion = True

    return TrialImportInitResponse(
        trial_id=trial_id,
        task_id=task_id,
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        trial_s3_key=trial_s3_key,
        archive_s3_key=archive_s3_key,
        upload_url=upload_url,
        upload_method=upload_method,
        upload_headers=upload_headers,
        requires_completion=requires_completion,
    )


async def complete_trial_import(
    *,
    trial_id: str,
    org_id: str | None = None,
) -> TrialImportCompleteResponse:
    """Extract the uploaded archive into the trial prefix and finalize."""
    async with get_session() as session:
        trial: TrialModel | None = await session.get(TrialModel, trial_id)
        if trial is None:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        if org_id is not None and trial.org_id != org_id:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        if trial.origin != TrialOrigin.IMPORTED:
            raise HTTPException(
                status_code=400,
                detail="complete may only be called for imported trials",
            )

        task_id = trial.task_id

    storage = get_storage_client()
    try:
        files_extracted = await storage.extract_trial_import_archive(trial_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to extract trial archive: {str(exc)}",
        ) from exc

    # After the artifacts are in place, nudge the task status forward
    # the same way the live trial handler does when a trial finishes.
    from oddish.queue import maybe_start_analysis_stage

    async with get_session() as session:
        await maybe_start_analysis_stage(session, trial_id)
        await session.commit()

        trial_again = await session.get(TrialModel, trial_id)
        trial_s3_key = (
            trial_again.trial_s3_key
            if trial_again and trial_again.trial_s3_key
            else StorageClient._trial_prefix(trial_id)
        )

    _ = task_id  # kept for future observability hooks
    return TrialImportCompleteResponse(
        trial_id=trial_id,
        trial_s3_key=trial_s3_key,
        files_extracted=files_extracted,
    )
