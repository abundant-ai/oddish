from collections import Counter
from contextlib import asynccontextmanager
import argparse
import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, select, delete
from typing import cast
import uvicorn
from rich.console import Console

from oddish.api.endpoints import (
    browse_tasks_core,
    get_task_status_core,
    get_task_version_core,
    get_trial_by_index_core,
    get_trial_for_org_core,
    list_task_versions_core,
    list_tasks_core,
    rerun_task_analysis_core,
    rerun_task_verdict_core,
    rerun_trial_analysis_core,
    retry_trial_core,
)
from oddish.api.public_helpers import (
    get_task_file_content_s3,
    get_trial_file_content_s3,
    list_task_files_s3,
    list_trial_files_s3,
)
from oddish.api.trial_io import (
    read_trial_agent_file,
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from oddish.api.admin import (
    QueueSlotsResponse,
    QueueStatusResponse,
    OrphanedStateResponse,
    get_queue_slots_core,
    get_queue_status_core,
    get_orphaned_state_core,
)
from oddish.api.dashboard import get_dashboard_core
from oddish.api.public import router as public_router
from oddish.api.tasks import handle_task_upload, resolve_task_storage
from oddish.config import settings
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    init_db,
    get_pool,
    utcnow,
)
from oddish.db.storage import collect_s3_prefixes_for_deletion, delete_s3_prefixes
from oddish.schemas import (
    TaskBatchCancelRequest,
    TaskBrowseResponse,
    ExperimentUpdateRequest,
    ExperimentUpdateResponse,
    TaskResponse,
    TaskStatusResponse,
    TaskSweepSubmission,
    TaskVersionResponse,
    TrialResponse,
    UploadResponse,
)
from oddish.task_timeouts import TaskTimeoutValidationError

from oddish.queue import (
    append_trials_to_task,
    cancel_tasks_runs,
    create_task,
)

console = Console()
logger = logging.getLogger(__name__)

_CONCURRENCY_OVERRIDES: dict[str, int] = {}


def get_queue_concurrency(queue_key: str) -> int:
    """Get concurrency limit for a queue key (with runtime overrides)."""
    overrides = _get_concurrency_overrides()
    normalized = settings.normalize_queue_key(queue_key)
    if normalized in overrides:
        return overrides[normalized]
    return cast(int, settings.get_model_concurrency(normalized))


def _get_concurrency_overrides() -> dict[str, int]:
    """Read concurrency overrides set at API startup."""
    return dict(_CONCURRENCY_OVERRIDES)


def update_queue_concurrency(overrides: dict[str, int]) -> None:
    """Update queue-key concurrency limits at API startup."""
    current = _get_concurrency_overrides()
    for queue_key, concurrency in overrides.items():
        # Take the max of current and new value
        normalized = settings.normalize_queue_key(queue_key)
        existing = current.get(normalized, 0)
        current[normalized] = max(existing, concurrency)
    _CONCURRENCY_OVERRIDES.clear()
    _CONCURRENCY_OVERRIDES.update(current)
    settings.model_concurrency_overrides = dict(current)
    console.print(f"[dim]Updated queue concurrency: {current}[/dim]")


async def _get_detached_trial(trial_id: str) -> TrialModel:
    """Load a trial, then release the DB session before artifact I/O."""
    async with get_session() as session:
        trial = await get_trial_for_org_core(session, trial_id=trial_id)
        session.expunge(trial)
        return trial


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup and optionally start workers."""
    # Ensure required storage directories exist
    Path(settings.harbor_jobs_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.local_storage_dir).mkdir(parents=True, exist_ok=True)

    await init_db()

    # Pre-warm the connection pool (so workers don't have to wait)
    # This ensures the pool is ready when workers start
    try:
        await get_pool()
    except Exception as e:
        # If pool creation fails, log but don't block API startup
        console.print(
            f"[yellow]Warning: Could not pre-warm connection pool: {e}[/yellow]"
        )

    worker_task = None
    if settings.auto_start_workers:
        from oddish.workers.queue.queue_manager import run_polling_worker

        async def start_workers():
            try:
                await asyncio.sleep(0.5)
                console.print("[green]Auto-starting queue workers...[/green]")
                await run_polling_worker()
            except asyncio.CancelledError:
                console.print("[yellow]Worker task cancelled[/yellow]")
            except Exception as e:
                console.print(f"[red]Worker error: {e}[/red]")

        worker_task = asyncio.create_task(start_workers())

    yield

    # Cleanup: cancel worker task if running
    if worker_task:
        console.print("[yellow]Shutting down workers...[/yellow]")
        worker_task.cancel()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        console.print("[green]Workers shut down[/green]")


api = FastAPI(
    title="Oddish - Eval Scheduler API",
    description="Task scheduler for Harbor eval tasks with multi-stage pipeline",
    version="0.2.0",
    lifespan=lifespan,
)

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api.include_router(public_router)


# =============================================================================
# Health & Status
# =============================================================================


@api.get("/health")
async def health():
    """Health check endpoint."""
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "timestamp": utcnow().isoformat(),
    }


# =============================================================================
# Dashboard
# =============================================================================


@api.get("/dashboard")
async def get_dashboard(
    tasks_limit: int = Query(200, ge=1, le=500),
    tasks_offset: int = Query(0, ge=0),
    experiments_limit: int = Query(25, ge=1, le=100),
    experiments_offset: int = Query(0, ge=0),
    experiments_query: str | None = Query(None),
    experiments_status: str = Query("all"),
    usage_minutes: int | None = Query(None, ge=1, le=86400),
    include_tasks: bool = Query(True),
    include_usage: bool = Query(True),
    include_experiments: bool = Query(True),
) -> dict:
    """Combined dashboard: queues, pipeline stats, model usage, tasks, and experiments."""
    async with get_session() as session:
        return await get_dashboard_core(
            session,
            tasks_limit=tasks_limit,
            tasks_offset=tasks_offset,
            experiments_limit=experiments_limit,
            experiments_offset=experiments_offset,
            experiments_query=experiments_query,
            experiments_status=experiments_status,
            usage_minutes=usage_minutes,
            include_tasks=include_tasks,
            include_usage=include_usage,
            include_experiments=include_experiments,
        )


# =============================================================================
# Task Upload & Submission Endpoints
# =============================================================================


@api.post("/tasks/upload", response_model=UploadResponse)
async def upload_task(
    file: UploadFile = File(...),
    content_hash: str | None = None,
    message: str | None = None,
) -> UploadResponse:
    """
    Upload a task directory (as tarball).

    The client should send a .tar.gz file containing the task directory.
    Server will extract it and store it (S3 if enabled, local directory otherwise).

    If a task with the same name already exists the server compares
    ``content_hash`` against the latest version.  When unchanged the existing
    version is reused; otherwise a new version is created automatically.

    Args:
        file: Tarball containing the task directory
        content_hash: Deterministic hash of the task directory contents
        message: Optional description of what changed in this version

    Returns:
        Upload response with task_id, version info, and storage location.
    """
    return await handle_task_upload(file, content_hash=content_hash, message=message)


# =============================================================================
# Task Endpoints
# =============================================================================


@api.post("/tasks/sweep", response_model=TaskResponse)
async def create_task_sweep(submission: TaskSweepSubmission):
    """
    Submit the common pattern: one task_id expanded into many trials.

    The task_id should be from a previous /tasks/upload call.
    The task files are already stored (S3 if enabled, local directory otherwise).
    """

    # Determine task path based on storage mode
    task_path, task_s3_key = await resolve_task_storage(
        submission.task_id,
        s3_missing_detail=(
            f"Task {submission.task_id} not found in S3. "
            "Upload it first with POST /tasks/upload"
        ),
        local_missing_detail=(
            f"Task {submission.task_id} not found in local storage. "
            "Upload it first with POST /tasks/upload"
        ),
    )

    from oddish.api.sweeps import (
        build_trial_specs_from_sweep,
        build_task_submission_from_sweep,
    )

    trials = build_trial_specs_from_sweep(submission)
    expanded = build_task_submission_from_sweep(
        submission,
        task_path=task_path,
        trials=trials,
    )

    async with get_session() as session:
        # Auto-detect append mode when the task already exists in the DB
        # (backward-compat with CLIs that don't send append_to_task).
        existing = await session.get(TaskModel, submission.task_id)
        if existing is not None:
            from oddish.queue import (
                get_experiment_by_id_or_name,
                get_or_create_experiment,
            )

            new_experiment_id: str | None = None
            if submission.experiment_id:
                exp = await get_experiment_by_id_or_name(
                    session, submission.experiment_id
                )
                if not exp:
                    exp = await get_or_create_experiment(
                        session, submission.experiment_id
                    )
                new_experiment_id = exp.id
            new_trials = await append_trials_to_task(
                session,
                task=existing,
                submission=expanded,
                experiment_id=new_experiment_id,
            )
            await session.commit()
            provider_counts: Counter[str] = Counter(t.provider for t in new_trials)
            return TaskResponse(
                id=existing.id,
                name=existing.name,
                status=existing.status,
                priority=existing.priority,
                trials_count=len(new_trials),
                providers=dict(provider_counts),
                created_at=existing.created_at,
            )

        try:
            task = await create_task(session, expanded, task_id=submission.task_id)
        except TaskTimeoutValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Store the S3 key if using S3
        if task_s3_key:
            task.task_s3_key = task_s3_key
            await session.commit()

        # Count trials per provider
        provider_counts = Counter()
        for trial in task.trials:
            provider_counts[trial.provider] += 1

        return TaskResponse(
            id=task.id,
            name=task.name,
            status=task.status,
            priority=task.priority,
            trials_count=len(task.trials),
            providers=dict(provider_counts),
            created_at=task.created_at,
        )


@api.get("/tasks", response_model=list[TaskStatusResponse])
async def list_tasks(
    status: str | None = None,
    user: str | None = None,
    experiment_id: str | None = None,
    include_trials: bool = True,
    limit: int = 100,
    offset: int = 0,
):
    """List all tasks with optional filtering."""
    async with get_session() as session:
        return await list_tasks_core(
            session,
            status=status,
            user=user,
            experiment_id=experiment_id,
            include_trials=include_trials,
            limit=limit,
            offset=offset,
            include_empty_rewards=False,
        )


@api.get("/tasks/browse", response_model=TaskBrowseResponse)
async def browse_tasks(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    query: str | None = None,
) -> TaskBrowseResponse:
    """Browse latest task versions with aggregated trial stats."""
    async with get_session() as session:
        return await browse_tasks_core(session, limit=limit, offset=offset, query=query)


@api.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """Get status of a task with all trials, analyses, and verdict."""
    async with get_session() as session:
        return await get_task_status_core(
            session,
            task_id=task_id,
            include_trials=True,
            include_empty_rewards=False,
        )


@api.get("/tasks/{task_id}/versions", response_model=list[TaskVersionResponse])
async def list_task_versions(task_id: str):
    """List all versions of a task, newest first."""
    async with get_session() as session:
        return await list_task_versions_core(session, task_id=task_id)


@api.get("/tasks/{task_id}/versions/{version}", response_model=TaskVersionResponse)
async def get_task_version(task_id: str, version: int):
    """Get a specific version of a task."""
    async with get_session() as session:
        return await get_task_version_core(session, task_id=task_id, version=version)


@api.post("/tasks/cancel")
async def cancel_tasks(payload: TaskBatchCancelRequest):
    """Cancel in-flight runs for many tasks without deleting data."""
    if not payload.task_ids:
        raise HTTPException(status_code=400, detail="Provide at least one task_id")

    async with get_session() as session:
        result = await cancel_tasks_runs(session, payload.task_ids)
        if result.get("error") == "not_found":
            raise HTTPException(status_code=404, detail="No matching tasks found")
        await session.commit()

    return {
        "status": "cancelled",
        "task_ids": result.get("task_ids", []),
        "not_found_task_ids": result.get("not_found_task_ids", []),
        "tasks_found": result.get("tasks_found", 0),
        "tasks_cancelled": result.get("tasks_cancelled", 0),
        "trials_cancelled": result.get("trials_cancelled", 0),
        "modal_calls_cancelled": 0,
    }


@api.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """Delete a task and its trials."""
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        task_rows = [(task.task_s3_key, task.task_path)]
        trial_rows_result = await session.execute(
            select(TrialModel.id, TrialModel.trial_s3_key).where(
                TrialModel.task_id == task.id
            )
        )
        trial_rows = [(row[0], row[1]) for row in trial_rows_result.all()]
        s3_prefixes = collect_s3_prefixes_for_deletion(
            tasks=task_rows,
            trials=trial_rows,
        )

        await session.delete(task)
        await session.commit()

    if s3_prefixes:
        try:
            await delete_s3_prefixes(s3_prefixes)
        except Exception:
            logger.exception("Failed to delete S3 artifacts for task %s", task_id)

    return {"status": "success", "deleted": {"task_id": task_id}}


@api.delete("/experiments/{experiment_id}")
async def delete_experiment(experiment_id: str):
    """Delete an experiment and all associated tasks/trials."""
    async with get_session() as session:
        experiment = await session.get(ExperimentModel, experiment_id)
        if not experiment:
            raise HTTPException(
                status_code=404, detail=f"Experiment {experiment_id} not found"
            )

        result = await session.execute(
            select(TaskModel.id).where(TaskModel.experiment_id == experiment_id)
        )
        task_ids = [row[0] for row in result.all()]
        task_rows_result = await session.execute(
            select(TaskModel.task_s3_key, TaskModel.task_path).where(
                TaskModel.experiment_id == experiment_id
            )
        )
        task_rows = [(row[0], row[1]) for row in task_rows_result.all()]
        trial_rows: list[tuple[str, str | None]] = []

        if task_ids:
            trial_rows_result = await session.execute(
                select(TrialModel.id, TrialModel.trial_s3_key).where(
                    TrialModel.task_id.in_(task_ids)
                )
            )
            trial_rows = [(row[0], row[1]) for row in trial_rows_result.all()]
            await session.execute(
                delete(TrialModel).where(TrialModel.task_id.in_(task_ids))
            )
            await session.execute(delete(TaskModel).where(TaskModel.id.in_(task_ids)))

        # Also delete trials linked only via trial.experiment_id
        trial_only_result = await session.execute(
            select(TrialModel.id, TrialModel.trial_s3_key).where(
                TrialModel.experiment_id == experiment_id,
            )
        )
        extra_trial_rows = [(r[0], r[1]) for r in trial_only_result.all()]
        trial_rows.extend(extra_trial_rows)
        if extra_trial_rows:
            await session.execute(
                delete(TrialModel).where(
                    TrialModel.experiment_id == experiment_id,
                )
            )

        s3_prefixes = collect_s3_prefixes_for_deletion(
            tasks=task_rows,
            trials=trial_rows,
        )

        await session.delete(experiment)
        await session.commit()

    if s3_prefixes:
        try:
            await delete_s3_prefixes(s3_prefixes)
        except Exception:
            logger.exception(
                "Failed to delete S3 artifacts for experiment %s", experiment_id
            )

    return {
        "status": "success",
        "deleted": {"experiment_id": experiment_id, "tasks": len(task_ids)},
    }


@api.patch("/experiments/{experiment_id}", response_model=ExperimentUpdateResponse)
async def update_experiment(
    experiment_id: str, payload: ExperimentUpdateRequest
) -> ExperimentUpdateResponse:
    """Update experiment metadata."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Experiment name cannot be empty")

    async with get_session() as session:
        experiment = await session.get(ExperimentModel, experiment_id)
        if not experiment:
            raise HTTPException(
                status_code=404, detail=f"Experiment {experiment_id} not found"
            )
        experiment.name = name
        await session.commit()

    return ExperimentUpdateResponse(id=experiment_id, name=name)


@api.get("/tasks/{task_id}/trials/{index}", response_model=TrialResponse)
async def get_trial(task_id: str, index: int):
    """Get a specific trial by its 0-based index within the task."""
    async with get_session() as session:
        return await get_trial_by_index_core(session, task_id=task_id, index=index)


# =============================================================================
# Analysis & Verdict Retry
# =============================================================================


@api.post("/tasks/{task_id}/analysis/retry")
async def retry_task_analysis(task_id: str) -> dict:
    """Queue analysis jobs for every completed trial in a task."""
    async with get_session() as session:
        return await rerun_task_analysis_core(session, task_id=task_id)


@api.post("/tasks/{task_id}/verdict/retry")
async def retry_task_verdict(task_id: str) -> dict:
    """Queue a fresh verdict job for a task whose analyses are complete."""
    async with get_session() as session:
        return await rerun_task_verdict_core(session, task_id=task_id)


@api.post("/trials/{trial_id}/retry")
async def retry_trial(trial_id: str) -> dict:
    """Re-queue a failed or completed trial for another attempt."""
    async with get_session() as session:
        return await retry_trial_core(session, trial_id=trial_id)


@api.post("/trials/{trial_id}/analysis/retry")
async def retry_trial_analysis(trial_id: str) -> dict:
    """Queue analysis for a completed trial and invalidate its task verdict."""
    async with get_session() as session:
        return await rerun_trial_analysis_core(session, trial_id=trial_id)


# =============================================================================
# Trial Artifact Endpoints
# =============================================================================


@api.get("/trials/{trial_id}/logs")
async def get_trial_logs(trial_id: str):
    """Get logs for a specific trial."""
    trial = await _get_detached_trial(trial_id)
    return await read_trial_logs(trial)


@api.get("/trials/{trial_id}/logs/structured")
async def get_trial_logs_structured(trial_id: str):
    """Get logs for a trial, structured by category (agent, verifier, exception)."""
    trial = await _get_detached_trial(trial_id)
    return await read_trial_logs_structured(trial)


@api.get("/trials/{trial_id}/trajectory")
async def get_trial_trajectory(trial_id: str):
    """Get ATIF trajectory.json for a trial (step-by-step agent actions)."""
    trial = await _get_detached_trial(trial_id)
    return await read_trial_trajectory(trial)


@api.get("/trials/{trial_id}/result")
async def get_trial_result(trial_id: str):
    """Get the full Harbor result.json for a trial."""
    trial = await _get_detached_trial(trial_id)
    return await read_trial_result(trial)


# =============================================================================
# File Access (S3 Storage)
# =============================================================================


@api.get("/tasks/{task_id}/files")
async def list_task_files(
    task_id: str,
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(True),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    """List all files in a task's S3 directory with optional presigned URLs."""
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        if version is None and task.current_version:
            version = task.current_version.version

    return await list_task_files_s3(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
        version=version,
    )


@api.get("/tasks/{task_id}/files/{file_path:path}")
async def get_task_file_content(
    task_id: str,
    file_path: str,
    presign: bool = Query(False),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    """Get content of a specific task file from S3."""
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        if version is None and task.current_version:
            version = task.current_version.version

    return await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
        version=version,
    )


@api.get("/trials/{trial_id}/files")
async def list_trial_files(trial_id: str) -> dict:
    """List all files in S3 for a trial, with presigned URLs for direct access."""
    trial = await _get_detached_trial(trial_id)
    return await list_trial_files_s3(trial)


@api.get("/trials/{trial_id}/files/{file_path:path}")
async def get_trial_file(trial_id: str, file_path: str) -> Response:
    """Get a file from a trial's S3 directory by relative path."""
    trial = await _get_detached_trial(trial_id)
    try:
        content, media_type = await get_trial_file_content_s3(trial, file_path)
        return Response(content=content, media_type=media_type)
    except HTTPException:
        pass
    content, media_type = await read_trial_agent_file(trial, file_path)
    return Response(content=content, media_type=media_type)


# =============================================================================
# Admin Diagnostics
# =============================================================================


@api.get("/admin/slots", response_model=QueueSlotsResponse)
async def admin_queue_slots() -> QueueSlotsResponse:
    """Get current state of queue-key slot leases."""
    async with get_session() as session:
        return await get_queue_slots_core(session)


@api.get("/admin/queue-status", response_model=QueueStatusResponse)
async def admin_queue_status() -> QueueStatusResponse:
    """Get queue status from the trials/tasks tables."""
    async with get_session() as session:
        return await get_queue_status_core(session)


@api.get("/admin/orphaned-state", response_model=OrphanedStateResponse)
async def admin_orphaned_state(
    stale_after_minutes: int = Query(10, ge=1, le=240),
) -> OrphanedStateResponse:
    """Summarize stale queue/pipeline state."""
    async with get_session() as session:
        return await get_orphaned_state_core(
            session, stale_after_minutes=stale_after_minutes
        )


def run_server(
    concurrency: dict[str, int] | None = None,
    host: str | None = None,
    port: int | None = None,
):
    """Start the API server.

    Args:
        concurrency: Queue concurrency limits (e.g., {"openai/gpt-5.2": 8})
        host: Override API host
        port: Override API port
    """
    # Apply concurrency settings if provided
    if concurrency:
        update_queue_concurrency(concurrency)

    uvicorn.run(
        "oddish.api:api",
        host=host or settings.api_host,
        port=port or settings.api_port,
        # IMPORTANT: auto-reload will restart the process on *any* file change and
        # cancels in-flight trials (shows up as Harbor TrialEvent.CANCEL).
        #
        # Use `oddish serve --reload` when you explicitly want reload semantics.
        reload=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oddish API server")
    parser.add_argument(
        "--n-concurrent",
        type=str,
        help="Queue concurrency as JSON (e.g., '{\"openai/gpt-5.2\": 8}')",
    )
    parser.add_argument("--host", type=str, help="API host")
    parser.add_argument("--port", type=int, help="API port")

    args = parser.parse_args()

    concurrency = None
    if args.n_concurrent:
        concurrency = json.loads(args.n_concurrent)

    run_server(concurrency=concurrency, host=args.host, port=args.port)
