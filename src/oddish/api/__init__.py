from collections import Counter
from contextlib import asynccontextmanager
import argparse
from pathlib import Path
import asyncio
import json

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, select, delete
from typing import cast
import uvicorn
from rich.console import Console

from oddish.api.endpoints import (
    get_task_status_core,
    get_trial_by_index_core,
    get_trial_logs_core,
    get_trial_result_core,
    list_tasks_core,
)
from oddish.api.tasks import handle_task_upload, resolve_task_storage
from oddish.config import Settings, settings
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    init_db,
    get_pool,
    utcnow,
)
from oddish.schemas import (
    ExperimentUpdateRequest,
    ExperimentUpdateResponse,
    TaskResponse,
    TaskStatusResponse,
    TaskSubmission,
    TaskSweepSubmission,
    TrialResponse,
    TrialSpec,
    UploadResponse,
)

from oddish.queue import (
    cancel_pgqueuer_jobs_for_tasks,
    cancel_pgqueuer_jobs_for_trials,
    create_task,
)
from oddish.workers import create_queue_manager

console = Console()

_CONCURRENCY_OVERRIDES: dict[str, int] = {}


def get_provider_concurrency(provider: str) -> int:
    """Get concurrency limit for a provider (with runtime overrides)."""
    overrides = _get_concurrency_overrides()
    if provider in overrides:
        return overrides[provider]
    return cast(int, settings.get_default_concurrency_for_provider(provider))


def _get_concurrency_overrides() -> dict[str, int]:
    """Read concurrency overrides set at API startup."""
    return dict(_CONCURRENCY_OVERRIDES)


def update_provider_concurrency(overrides: dict[str, int]) -> None:
    """Update provider concurrency limits at API startup."""
    current = _get_concurrency_overrides()
    for provider, concurrency in overrides.items():
        # Take the max of current and new value
        existing = current.get(provider, 0)
        current[provider] = max(existing, concurrency)
    _CONCURRENCY_OVERRIDES.clear()
    _CONCURRENCY_OVERRIDES.update(current)
    Settings.default_provider_concurrency = dict(current)
    console.print(f"[dim]Updated provider concurrency: {current}[/dim]")


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

    # Start workers automatically if enabled (in background, non-blocking)
    worker_task = None
    if settings.auto_start_workers:
        # Start worker initialization in background (don't block API startup)
        async def start_workers():
            try:
                # Small delay to let API server start responding first
                await asyncio.sleep(0.5)

                console.print("[green]Auto-starting PGQueuer workers...[/green]")

                # Create queue manager using the same function as standalone worker
                # Pool is already warmed up, so this should be fast
                qm = await create_queue_manager()

                console.print("[blue]Registered provider queues:[/blue]")
                console.print("  - claude (trials + analysis + verdict)")
                console.print("  - gemini (trials)")
                console.print("  - openai (trials - includes codex, gpt)")
                console.print("  - default (trials - oracle, etc)")
                console.print(
                    f"[dim]Provider concurrency: {_get_concurrency_overrides() or settings.default_provider_concurrency}[/dim]"
                )

                # Run the queue manager (this blocks, but that's OK in background task)
                # verify_structure() runs here, but it's in background so API is already responding
                await qm.run()
            except asyncio.CancelledError:
                console.print("[yellow]Worker task cancelled[/yellow]")
            except Exception as e:
                console.print(f"[red]Worker error: {e}[/red]")
                # Don't raise - log and continue (API server should keep running)

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
# Task Upload & Submission Endpoints
# =============================================================================


@api.post("/tasks/upload", response_model=UploadResponse)
async def upload_task(file: UploadFile = File(...)) -> UploadResponse:
    """
    Upload a task directory (as tarball).

    The client should send a .tar.gz file containing the task directory.
    Server will extract it and store it (S3 if enabled, local directory otherwise).

    Args:
        file: Tarball containing the task directory

    Returns:
        {"task_id": "abc123", "s3_key": "tasks/abc123/"} (if S3 enabled)
        {"task_id": "abc123", "task_path": "/path/to/task"} (if S3 disabled)
    """
    return await handle_task_upload(file)


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

    trials: list[TrialSpec] = []

    for config in submission.configs:
        for _ in range(config.n_trials):
            trials.append(
                TrialSpec(
                    agent=config.agent,
                    model=config.model,
                    timeout_minutes=config.timeout_minutes
                    or submission.timeout_minutes,
                    environment=config.environment or submission.environment,
                    agent_env=config.agent_env,
                    agent_kwargs=config.agent_kwargs,
                )
            )

    expanded = TaskSubmission(
        task_path=task_path,  # Local path or s3:// URL
        trials=trials,
        user=submission.user,
        priority=submission.priority,
        experiment_id=submission.experiment_id,
        tags=submission.tags,
        run_analysis=submission.run_analysis,
        disable_verification=submission.disable_verification,
        verifier_timeout_sec=submission.verifier_timeout_sec,
        env_cpus=submission.env_cpus,
        env_memory_mb=submission.env_memory_mb,
        env_storage_mb=submission.env_storage_mb,
        env_gpus=submission.env_gpus,
        env_gpu_types=submission.env_gpu_types,
        allow_internet=submission.allow_internet,
        agent_setup_timeout_sec=submission.agent_setup_timeout_sec,
        docker_image=submission.docker_image,
        mcp_servers=submission.mcp_servers,
        artifacts=submission.artifacts,
        sandbox_timeout_secs=submission.sandbox_timeout_secs,
        sandbox_idle_timeout_secs=submission.sandbox_idle_timeout_secs,
        auto_stop_interval_mins=submission.auto_stop_interval_mins,
        auto_delete_interval_mins=submission.auto_delete_interval_mins,
        snapshot_template_name=submission.snapshot_template_name,
    )

    async with get_session() as session:
        try:
            task = await create_task(session, expanded, task_id=submission.task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Store the S3 key if using S3
        if task_s3_key:
            task.task_s3_key = task_s3_key
            await session.commit()

        # Count trials per provider
        provider_counts: Counter[str] = Counter()
        for trial in task.trials:
            provider_counts[trial.provider] += 1

        return TaskResponse(
            id=task.id,
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


@api.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """Delete a task and its trials."""
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        trial_ids_result = await session.execute(
            select(TrialModel.id).where(TrialModel.task_id == task.id)
        )
        trial_ids = [row[0] for row in trial_ids_result.all()]
        await cancel_pgqueuer_jobs_for_trials(session, trial_ids)
        await cancel_pgqueuer_jobs_for_tasks(session, [task.id])

        await session.delete(task)
        await session.commit()

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

        if task_ids:
            trial_ids_result = await session.execute(
                select(TrialModel.id).where(TrialModel.task_id.in_(task_ids))
            )
            trial_ids = [row[0] for row in trial_ids_result.all()]
            await cancel_pgqueuer_jobs_for_trials(session, trial_ids)
            await cancel_pgqueuer_jobs_for_tasks(session, task_ids)

            await session.execute(
                delete(TrialModel).where(TrialModel.task_id.in_(task_ids))
            )
            await session.execute(delete(TaskModel).where(TaskModel.id.in_(task_ids)))

        await session.delete(experiment)
        await session.commit()

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
# S3 Storage Endpoints
# =============================================================================


@api.get("/trials/{trial_id}/logs")
async def get_trial_logs(trial_id: str):
    """Get logs for a specific trial (from S3 if enabled, otherwise from local storage)."""
    async with get_session() as session:
        return await get_trial_logs_core(session, trial_id=trial_id)


@api.get("/trials/{trial_id}/result")
async def get_trial_result(trial_id: str):
    """Get the full Harbor result.json for a trial (from S3 if enabled, otherwise from local storage)."""
    async with get_session() as session:
        return await get_trial_result_core(session, trial_id=trial_id)


def run_server(
    concurrency: dict[str, int] | None = None,
    host: str | None = None,
    port: int | None = None,
):
    """Start the API server.

    Args:
        concurrency: Provider concurrency limits (e.g., {"claude": 8, "default": 4})
        host: Override API host
        port: Override API port
    """
    # Apply concurrency settings if provided
    if concurrency:
        update_provider_concurrency(concurrency)

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
        help="Provider concurrency as JSON (e.g., '{\"claude\": 8}')",
    )
    parser.add_argument("--host", type=str, help="API host")
    parser.add_argument("--port", type=int, help="API port")

    args = parser.parse_args()

    concurrency = None
    if args.n_concurrent:
        concurrency = json.loads(args.n_concurrent)

    run_server(concurrency=concurrency, host=args.host, port=args.port)
