from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from oddish.config import settings
from oddish.db import AnalysisStatus, TaskModel, utcnow
from oddish.db.storage import resolve_task_directory, resolve_trial_directory
from oddish.workers.queue.db_helpers import _trial_session
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

ANALYSIS_TIMEOUT = 900  # 15 minutes
# Keep comfortably below ``STALE_HEARTBEAT_MINUTES`` (15 min) so a
# slow classification can't drift across the reap threshold mid-run.
# 30s matches the trial heartbeat interval for consistency.
ANALYSIS_HEARTBEAT_INTERVAL_SECONDS = 30


async def _heartbeat_analysis_worker_job(
    *,
    worker_job_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Keep ``worker_jobs.heartbeat_at`` fresh during a slow classification.

    Analysis used to run entirely between the claim (which stamps
    ``heartbeat_at``) and the outcome record (which finalizes the
    row). With ``ANALYSIS_TIMEOUT`` = ``STALE_HEARTBEAT_MINUTES`` a
    15-minute-ish classification was within a rounding error of the
    reap threshold. This loop writes every 30s so it can't happen.

    Same failure-tolerance pattern as the trial heartbeat: a DB write
    failure bumps the heartbeat_failure_count / last_heartbeat_error
    breadcrumb for post-mortem, but never crashes the analysis.
    """
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=ANALYSIS_HEARTBEAT_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass

        if stop_event.is_set():
            return

        try:
            await heartbeat_worker_job(
                worker_job_id,
                pending_failure_count=pending_failure_count,
                pending_last_error=pending_last_error,
            )
            if consecutive_failures > 0:
                console.print(
                    f"[green]Analysis worker_job {worker_job_id} heartbeat "
                    f"recovered after {consecutive_failures} failure(s)[/green]"
                )
            consecutive_failures = 0
            pending_failure_count = 0
            pending_last_error = None
        except Exception as exc:
            consecutive_failures += 1
            pending_failure_count += 1
            pending_last_error = f"{type(exc).__name__}: {exc}"


async def run_analysis_job(
    trial_id: str,
    queue_key: str,
    modal_function_call_id: str | None = None,
    worker_job_id: str | None = None,
) -> None:
    """
    Execute analysis for a claimed trial.

    1. Download task and trial from S3
    2. Run classification with Claude Code
    3. Store classification in trial.analysis
    4. Check if all analyses done -> start verdict stage
    """
    from oddish.analyze import TrialClassifier

    console.print(
        f"[cyan]Processing analysis[/cyan] {trial_id} (queue_key={queue_key})"
    )
    console.print(f"[dim]Task bucket: {settings.s3_bucket}[/dim]")

    # Mark as running
    async with _trial_session(trial_id) as (session, trial):
        if not trial:
            raise RuntimeError(f"Trial {trial_id} not found in database")

        # Skip if already analyzed
        if trial.analysis_status in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED):
            console.print(
                f"[yellow]Trial {trial_id} already analyzed, skipping[/yellow]"
            )
            return

        trial.analysis_status = AnalysisStatus.RUNNING
        trial.analysis_started_at = utcnow()

        # Get task info for downloads
        task = await session.get(TaskModel, trial.task_id)
        if not task:
            raise RuntimeError(f"Task {trial.task_id} not found")

        task_s3_key = task.task_s3_key
        trial_s3_key = trial.trial_s3_key
        task_path = task.task_path
        trial_result_path = trial.harbor_result_path
        trial_agent = trial.agent

        # Log storage locations for debugging
        console.print(f"[dim]Task S3 key: {task_s3_key or '(not set)'}[/dim]")
        console.print(f"[dim]Trial S3 key: {trial_s3_key or '(not set)'}[/dim]")
        console.print(f"[dim]Task local path: {task_path or '(not set)'}[/dim]")
        console.print(
            f"[dim]Trial local path: {trial_result_path or '(not set)'}[/dim]"
        )

    # Resolve task and trial directories (S3 or local)
    temp_task_dir = None
    temp_trial_dir = None
    task_dir_to_use: Path | None = None
    trial_dir_to_use: Path | None = None
    classification_result = None
    analysis_error = None

    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if worker_job_id:
        heartbeat_task = asyncio.create_task(
            _heartbeat_analysis_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )

    try:
        (
            task_dir_to_use,
            temp_task_dir,
            resolved_task_s3_key,
        ) = await resolve_task_directory(
            task_id=task.id,
            task_s3_key=task_s3_key,
            task_path=task_path,
        )
        if temp_task_dir:
            console.print(f"[dim]Downloaded task from S3: {resolved_task_s3_key}[/dim]")
        else:
            console.print(f"[dim]Using local task path: {task_dir_to_use}[/dim]")

        (
            trial_dir_to_use,
            temp_trial_dir,
            resolved_trial_s3_key,
        ) = await resolve_trial_directory(
            trial_id=trial_id,
            trial_s3_key=trial_s3_key,
            trial_result_path=trial_result_path,
        )
        if temp_trial_dir:
            console.print(
                f"[dim]Downloaded trial from S3: {resolved_trial_s3_key}[/dim]"
            )
        else:
            console.print(f"[dim]Using local trial path: {trial_dir_to_use}[/dim]")

        # Run classification
        classifier = TrialClassifier(
            model=settings.analysis_model,
            verbose=True,
            timeout=ANALYSIS_TIMEOUT,  # 5 minutes
        )

        console.print(f"[cyan]Running classification for {trial_id}...[/cyan]")
        classification = await classifier.classify_trial(
            trial_dir=trial_dir_to_use,
            task_dir=task_dir_to_use,
            trial_agent=trial_agent,
        )

        # Convert to dict for storage
        classification_result = {
            "trial_name": classification.trial_name,
            "classification": classification.classification.value,
            "subtype": classification.subtype,
            "evidence": classification.evidence,
            "root_cause": classification.root_cause,
            "recommendation": classification.recommendation,
            "reward": classification.reward,
        }

        # Check if classification is a fallback (indicates Claude SDK issue)
        if "classification failed" in (classification.evidence or "").lower():
            console.print(
                f"[yellow]Classification used fallback for {trial_id}:[/yellow] {classification.evidence}"
            )
        else:
            console.print(
                f"[green]Classification complete:[/green] {classification.classification.value} - {classification.subtype}"
            )

    except asyncio.CancelledError:
        analysis_error = (
            "Analysis was cancelled by the worker runtime before it finished. "
            "This is usually caused by a worker restart or shutdown."
        )
        console.print(f"[yellow]Analysis cancelled for {trial_id}[/yellow]")
    except Exception as e:
        analysis_error = f"{type(e).__name__}: {e}"
        console.print(f"[red]Analysis error for {trial_id}: {analysis_error}[/red]")
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        # Clean up temp directories
        if temp_task_dir and temp_task_dir.exists():
            shutil.rmtree(temp_task_dir, ignore_errors=True)
        if temp_trial_dir and temp_trial_dir.exists():
            shutil.rmtree(temp_trial_dir, ignore_errors=True)

    async def _store_results() -> None:
        async with _trial_session(trial_id, allow_missing=True) as (session, trial):
            if not trial:
                return

            if classification_result:
                trial.analysis = classification_result
                trial.analysis_status = AnalysisStatus.SUCCESS
                trial.analysis_finished_at = utcnow()
                trial.analysis_error = None
                console.print(f"[green]Analysis {trial_id} SUCCESS[/green]")
            else:
                trial.analysis_status = AnalysisStatus.FAILED
                trial.analysis_error = (
                    analysis_error or "Analysis execution failed with exception"
                )
                trial.analysis_finished_at = utcnow()
                console.print(f"[red]Analysis {trial_id} FAILED[/red]")

            # Check if all analyses done → start verdict stage.
            # Imported lazily to avoid a circular import with
            # ``oddish.queue`` (handler auto-registration path).
            from oddish.queue import maybe_start_verdict_stage

            started = await maybe_start_verdict_stage(session, trial_id)
            if started:
                console.print(
                    f"[blue]Task {trial.task_id} transitioned to VERDICT_PENDING[/blue]"
                )

    await asyncio.shield(_store_results())
