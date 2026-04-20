from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import json
import os
import shutil
import uuid
from pathlib import Path

from harbor.models.environment_type import EnvironmentType
from harbor.trial.hooks import TrialEvent, TrialHookEvent
from harbor.viewer.scanner import JobScanner

from oddish.config import settings
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialStatus,
    utcnow,
)
from oddish.db.storage import get_storage_client, resolve_task_directory
from oddish.workers.harbor_runner import HarborOutcome, run_harbor_trial_async
from oddish.workers.queue.db_helpers import _trial_session
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

TRIAL_HEARTBEAT_INTERVAL_SECONDS = 30


@dataclass(slots=True)
class PreparedTrialRun:
    task_path: str | None
    task_s3_key: str | None
    task_id: str
    trial_agent: str
    trial_model: str
    trial_environment: str | None
    trial_harbor_config: dict | None


@dataclass(slots=True)
class TrialExecutionResult:
    outcome: HarborOutcome | None
    execution_error: str | None


def _is_agent_timeout_exception(exc: object | None) -> bool:
    return bool(exc and getattr(exc, "exception_type", None) == "AgentTimeoutError")


def _is_agent_timeout_error_message(error: str | None) -> bool:
    if not error:
        return False
    return "AgentTimeoutError" in error or "Agent execution timed out" in error


def _verifier_ran_from_job_result(job_result_path: str | None) -> bool:
    if not job_result_path:
        return False
    try:
        result_path = Path(job_result_path)
        if not result_path.exists():
            return False

        # Backward compatibility: older Harbor job result.json included trial_results.
        data = json.loads(result_path.read_text(encoding="utf-8"))
        trial_results = data.get("trial_results") if isinstance(data, dict) else None
        if isinstance(trial_results, list):
            for trial_result in trial_results:
                if (
                    isinstance(trial_result, dict)
                    and trial_result.get("verifier_result") is not None
                ):
                    return True

        job_dir = result_path.parent
        scanner = JobScanner(job_dir.parent)
        for trial_name in scanner.list_trials(job_dir.name):
            trial_result = scanner.get_trial_result(job_dir.name, trial_name)
            if trial_result and trial_result.verifier_result is not None:
                return True
    except Exception:
        return False
    return False


def _cleanup_uploaded_job_dir(job_dir: Path | None, trial_id: str) -> None:
    """Delete local Harbor artifacts after a successful S3 upload."""
    if not job_dir:
        return
    try:
        base_dir = Path(settings.harbor_jobs_dir).resolve()
        resolved_job_dir = job_dir.resolve()
        if not resolved_job_dir.exists():
            return
        if not resolved_job_dir.is_relative_to(base_dir):
            console.print(
                "[yellow]Skipping cleanup outside harbor_jobs_dir for "
                f"{trial_id}: {resolved_job_dir}[/yellow]"
            )
            return
        shutil.rmtree(resolved_job_dir, ignore_errors=True)
        console.print(
            f"[dim]Cleaned local Harbor artifacts for {trial_id}: {resolved_job_dir}[/dim]"
        )
    except Exception as e:
        console.print(f"[yellow]Failed to cleanup local Harbor artifacts: {e}[/yellow]")


# Maximum length we persist for last_heartbeat_error. We truncate aggressively
# because it's for operator diagnosis, not full stack traces.
_HEARTBEAT_ERROR_MAX_LEN = 500


async def _touch_trial_execution(
    *,
    trial_id: str,
    worker_id: str | None,
    queue_slot: int | None,
    claimed: bool = False,
    pending_failure_count: int = 0,
    pending_last_error: str | None = None,
    pending_last_error_at: datetime | None = None,
) -> None:
    """Update the trial's heartbeat state.

    When pending_failure_count > 0, we also flush accumulated heartbeat-write
    failure metadata into the row. This is how a recovered worker tells the DB
    "by the way, I tried to write N heartbeats during the last outage and
    they all failed with $error" -- which lets operators distinguish a real
    worker crash (no recovery) from a DB/pooler outage (counter bumps, then
    heartbeats resume).
    """
    async with _trial_session(trial_id, allow_missing=True) as (session, trial):
        if not trial or trial.status != TrialStatus.RUNNING:
            return
        if worker_id and trial.current_worker_id not in (None, worker_id):
            return

        now = utcnow()
        trial.current_worker_id = worker_id
        trial.current_queue_slot = queue_slot
        if claimed:
            trial.claimed_at = now
        trial.heartbeat_at = now

        if pending_failure_count > 0:
            trial.heartbeat_failure_count = (
                trial.heartbeat_failure_count or 0
            ) + pending_failure_count
            if pending_last_error is not None:
                trial.last_heartbeat_error = pending_last_error[
                    :_HEARTBEAT_ERROR_MAX_LEN
                ]
            if pending_last_error_at is not None:
                trial.last_heartbeat_error_at = pending_last_error_at


async def _heartbeat_trial_execution(
    *,
    trial_id: str,
    worker_id: str | None,
    queue_slot: int | None,
    stop_event: asyncio.Event,
    worker_job_id: str | None = None,
) -> None:
    """Periodically write heartbeat_at to keep the trial out of stale-reap.

    Writes to *both* tables every tick:
    - ``trials.heartbeat_at`` (domain-state denorm used by live UI)
    - ``worker_jobs.heartbeat_at`` (scheduling-state, read by the
      stale-reap sweep)

    The unified stale-reap in ``cleanup.py`` reads only ``worker_jobs``,
    so missing the worker_jobs write would cause long-running trials
    (Harbor can run for hours) to get falsely reaped after the 15-minute
    threshold. Kept as two separate writes rather than a single txn
    because a pooler blip on one shouldn't silence heartbeats on the
    other; the failure-folding behavior below applies uniformly.

    If the DB write fails we DO NOT crash the trial -- the underlying work
    can continue. We accumulate failure info locally and flush it on the
    next successful write so operators can tell after the fact whether a
    stale-heartbeat reap was caused by (a) the worker dying silently or
    (b) the DB/pooler being unreachable for a stretch.
    """
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None
    pending_last_error_at: datetime | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=TRIAL_HEARTBEAT_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass

        if stop_event.is_set():
            return

        try:
            await _touch_trial_execution(
                trial_id=trial_id,
                worker_id=worker_id,
                queue_slot=queue_slot,
                pending_failure_count=pending_failure_count,
                pending_last_error=pending_last_error,
                pending_last_error_at=pending_last_error_at,
            )
            if worker_job_id:
                # Second write to worker_jobs.heartbeat_at. This is what
                # the stale-reap sweep actually reads, so missing it
                # falsely reaps healthy trials after 15 minutes.
                await heartbeat_worker_job(
                    worker_job_id,
                    pending_failure_count=pending_failure_count,
                    pending_last_error=pending_last_error,
                )
            if consecutive_failures > 0:
                console.print(
                    f"[green]Trial {trial_id} heartbeat recovered after "
                    f"{consecutive_failures} consecutive failure(s)[/green]"
                )
            consecutive_failures = 0
            pending_failure_count = 0
            pending_last_error = None
            pending_last_error_at = None
        except Exception as exc:
            consecutive_failures += 1
            pending_failure_count += 1
            pending_last_error = f"{type(exc).__name__}: {exc}"
            pending_last_error_at = utcnow()
            console.print(
                f"[yellow]Trial {trial_id} heartbeat write failed "
                f"(consecutive={consecutive_failures}): {exc}[/yellow]"
            )


async def _prepare_trial_run(
    *,
    trial_id: str,
    worker_id: str | None,
    queue_slot: int | None,
    modal_function_call_id: str | None,
) -> PreparedTrialRun | None:
    # Split session usage - quick DB update, then release connection
    async with _trial_session(trial_id) as (session, trial):
        if not trial:
            console.print(f"[yellow]Trial {trial_id} not found, skipping[/yellow]")
            return None

        # Clear terminal state from any previous attempt before starting a retry.
        trial.status = TrialStatus.RUNNING
        trial.started_at = utcnow()
        trial.finished_at = None
        trial.next_retry_at = None
        trial.harbor_stage = "starting"  # Initial stage before Harbor events
        trial.reward = None
        trial.error_message = None
        trial.harbor_result_path = None
        trial.trial_s3_key = None
        trial.result = None
        trial.input_tokens = None
        trial.cache_tokens = None
        trial.output_tokens = None
        trial.cost_usd = None
        trial.phase_timing = None
        trial.has_trajectory = False
        trial.attempts += 1

        # Set idempotency key on first attempt
        if not trial.idempotency_key:
            trial.idempotency_key = str(uuid.uuid4())

        # Update task status if needed
        task = await session.get(TaskModel, trial.task_id)
        if task and task.status == TaskStatus.PENDING:
            task.status = TaskStatus.RUNNING
            task.started_at = utcnow()

        task_id = task.id if task else trial.task_id

        # Prefer the version-specific path so the worker runs the exact
        # content the trial was created against.
        task_path: str | None = None
        task_s3_key: str | None = None
        if trial.task_version_id:
            tv = await session.get(TaskVersionModel, trial.task_version_id)
            if tv:
                task_path = tv.task_path
                task_s3_key = tv.task_s3_key
        if task_path is None and task:
            task_path = task.task_path
        if task_s3_key is None and task:
            task_s3_key = task.task_s3_key
        trial_agent = trial.agent
        trial_model = settings.normalize_trial_model(trial_agent, trial.model)
        if trial.model != trial_model:
            trial.model = trial_model
        canonical_queue_key = settings.get_queue_key_for_trial(trial_agent, trial_model)
        if trial.queue_key != canonical_queue_key:
            trial.queue_key = canonical_queue_key
        trial_environment = trial.environment
        trial_harbor_config = trial.harbor_config
        trial.current_worker_id = worker_id
        trial.current_queue_slot = queue_slot
        trial.modal_function_call_id = modal_function_call_id
        trial.claimed_at = utcnow()
        trial.heartbeat_at = trial.claimed_at

        return PreparedTrialRun(
            task_path=task_path,
            task_s3_key=task_s3_key,
            task_id=task_id,
            trial_agent=trial_agent,
            trial_model=trial_model,
            trial_environment=trial_environment,
            trial_harbor_config=trial_harbor_config,
        )


async def _store_trial_results(
    *,
    trial_id: str,
    outcome: HarborOutcome | None,
    trial_s3_key: str | None,
    execution_error: str | None,
) -> None:
    async with _trial_session(trial_id, allow_missing=True) as (session, trial):
        if not trial:
            return

        # If the trial was cancelled by the user while we were running,
        # don't overwrite its FAILED/"Cancelled by user" state.
        # The cancel API sets error_message and also max_attempts=attempts
        # as a reliable signal (survives even if this code is from an older deploy).
        if (
            trial.error_message == "Cancelled by user"
            or trial.harbor_stage == "cancelled"
            or (
                trial.status == TrialStatus.FAILED
                and trial.max_attempts <= trial.attempts
            )
        ):
            console.print(
                f"[dim]Trial {trial_id} was cancelled by user, skipping result update[/dim]"
            )
            return

        if outcome:
            # Always update reward/error/paths from outcome (most authoritative source)
            is_timeout = _is_agent_timeout_error_message(outcome.error)
            derived_reward = outcome.reward
            if derived_reward is None and is_timeout:
                verifier_ran = _verifier_ran_from_job_result(
                    str(outcome.job_result_path) if outcome.job_result_path else None
                )
                if verifier_ran:
                    derived_reward = 0.0
                    console.print(
                        f"[yellow]Trial {trial_id} agent timeout -> reward=0[/yellow]"
                    )

            trial.reward = derived_reward
            if outcome.error:
                trial.error_message = outcome.error
            elif derived_reward is not None:
                trial.error_message = None
            trial.harbor_result_path = (
                str(outcome.job_result_path) if outcome.job_result_path else None
            )
            trial.trial_s3_key = trial_s3_key

            # Store token usage & cost from Harbor's AgentContext
            trial.input_tokens = outcome.input_tokens
            trial.cache_tokens = outcome.cache_tokens
            trial.output_tokens = outcome.output_tokens
            trial.cost_usd = outcome.cost_usd

            # Store per-phase timing breakdown
            trial.phase_timing = outcome.phase_timing

            # Store trajectory availability
            trial.has_trajectory = outcome.has_trajectory

            # SUCCESS means "trial executed to completion" (regardless of reward)
            # FAILED means "trial encountered an execution error"
            if derived_reward is not None:
                # Harbor produced a verifier score - trial executed successfully.
                # Hook may have already set status to SUCCESS - that's OK, we're confirming it
                trial.status = TrialStatus.SUCCESS
                trial.finished_at = utcnow()
                console.print(
                    f"[green]Trial {trial_id} SUCCESS[/green] reward={derived_reward}"
                )
            else:
                # No reward - trial encountered an error or didn't complete verification.
                if trial.attempts < trial.max_attempts:
                    trial.status = TrialStatus.RETRYING
                    console.print(
                        f"[yellow]Trial {trial_id} re-queued for retry "
                        f"({trial.attempts}/{trial.max_attempts})[/yellow]"
                    )
                else:
                    trial.status = TrialStatus.FAILED
                    trial.finished_at = utcnow()
                    console.print(f"[red]Trial {trial_id} FAILED (max attempts)[/red]")
        else:
            trial.status = TrialStatus.FAILED
            trial.finished_at = utcnow()
            trial.error_message = (
                execution_error or "Trial execution failed with exception"
            )
            console.print(f"[red]Trial {trial_id} FAILED (exception)[/red]")

        trial.current_worker_id = None
        trial.current_queue_slot = None
        trial.modal_function_call_id = None
        trial.heartbeat_at = utcnow()

        if trial.status in (TrialStatus.SUCCESS, TrialStatus.FAILED):
            task = await session.get(TaskModel, trial.task_id)
            # Check if all trials done → transition task status.
            # Imported lazily to avoid a circular import with
            # ``oddish.queue`` (which imports the worker_jobs enqueue
            # helpers, which in turn import this module via the handler
            # auto-registration).
            from oddish.queue import (
                _enqueue_analysis_worker_job,
                maybe_start_analysis_stage,
            )

            if task and task.run_analysis and trial.analysis_status is None:
                trial.analysis_status = AnalysisStatus.QUEUED
                trial.analysis_modal_function_call_id = None
                await _enqueue_analysis_worker_job(
                    session, trial_id=trial_id, org_id=trial.org_id
                )
                console.print(f"[cyan]Queued analysis for {trial_id}[/cyan]")

            started = await maybe_start_analysis_stage(session, trial_id)
            if started:
                console.print(
                    f"[blue]Task {trial.task_id} transitioned to next stage[/blue]"
                )


async def _handle_harbor_event(
    hook_event: TrialHookEvent,
    *,
    trial_id: str,
) -> None:
    """Update database when Harbor trial lifecycle events occur."""
    event = hook_event.event
    try:
        async with _trial_session(trial_id, allow_missing=True) as (_session, trial):
            if not trial:
                return

            # If the trial was cancelled by the user (cancel API sets
            # max_attempts=attempts), don't let lifecycle hooks
            # overwrite the "Cancelled by user" error_message/stage.
            user_cancelled = trial.error_message == "Cancelled by user" or (
                trial.status == TrialStatus.FAILED
                and trial.max_attempts <= trial.attempts
                and trial.max_attempts > 0
            )
            if user_cancelled and event in (
                TrialEvent.END,
                TrialEvent.CANCEL,
            ):
                console.print(
                    f"[dim]Trial {trial_id} event {event.value} "
                    f"ignored (cancelled by user)[/dim]"
                )
                return

            # Log event
            console.print(f"[dim]Trial {trial_id} event: {event.value}[/dim]")
            trial.heartbeat_at = utcnow()

            # Update database based on event type
            if event == TrialEvent.START:
                # Trial started - already handled before Harbor execution
                trial.harbor_stage = "trial_started"
            elif event == TrialEvent.ENVIRONMENT_START:
                # Environment is ready
                trial.harbor_stage = "environment_setup"
                console.print(
                    f"[dim cyan]Trial {trial_id} environment started[/dim cyan]"
                )
            elif event == TrialEvent.AGENT_START:
                # Agent began execution
                trial.harbor_stage = "agent_running"
                console.print(f"[cyan]Trial {trial_id} agent started[/cyan]")
            elif event == TrialEvent.VERIFICATION_START:
                # Verification started
                trial.harbor_stage = "verification"
                console.print(
                    f"[dim cyan]Trial {trial_id} verification started[/dim cyan]"
                )
            elif event == TrialEvent.END:
                # Trial ended (success or failure) - extract result data
                trial.harbor_stage = "completed"

                # Extract result data
                extracted_reward = None
                has_error = False
                if hook_event.result:
                    result = hook_event.result
                    if result.verifier_result and result.verifier_result.rewards:
                        reward_value = result.verifier_result.rewards.get("reward")
                        if reward_value is not None:
                            extracted_reward = float(reward_value)
                            console.print(
                                f"[dim]Trial {trial_id} reward: {extracted_reward}[/dim]"
                            )

                    # Store exception info if present
                    if result.exception_info:
                        exc = result.exception_info
                        error_msg = (
                            exc.exception_message
                            or exc.exception_type
                            or "Unknown error"
                        )
                        is_agent_timeout = _is_agent_timeout_exception(exc)
                        if is_agent_timeout:
                            if (
                                extracted_reward is None
                                and result.verifier_result is not None
                            ):
                                # Agent timeout is a normal trial failure (reward=0).
                                extracted_reward = 0.0
                            # Keep error message for transparency, but don't mark as harness error.
                            if extracted_reward is not None:
                                trial.error_message = str(error_msg)
                            else:
                                trial.error_message = str(error_msg)
                                has_error = True
                        else:
                            trial.error_message = str(error_msg)
                            has_error = True

                # Set status here to ensure correctness even if worker crashes
                # before the final status update. The final update can still
                # override this if needed (e.g., if outcome has a reward).
                if extracted_reward is not None:
                    trial.status = TrialStatus.SUCCESS
                    trial.reward = extracted_reward
                    trial.finished_at = utcnow()
                elif has_error:
                    # Mark as failed if there's an error - prevents orphaned
                    # "running" trials if worker times out after this hook
                    trial.status = TrialStatus.FAILED
                    trial.finished_at = utcnow()

                console.print(
                    f"[dim]Trial {trial_id} ended, reward={extracted_reward}, error={has_error}[/dim]"
                )
            elif event == TrialEvent.CANCEL:
                # Trial cancelled
                trial.harbor_stage = "cancelled"
                trial.status = TrialStatus.FAILED
                trial.error_message = (
                    "Trial cancelled by the runtime. This is usually caused by a "
                    "worker restart or an environment startup failure. Check worker logs."
                )
                trial.finished_at = utcnow()
                console.print(f"[yellow]Trial {trial_id} cancelled[/yellow]")

    except Exception as e:
        console.print(f"[yellow]Hook callback error: {e}[/yellow]")


async def _execute_trial(
    *,
    trial_id: str,
    task_path_to_run: Path,
    temp_task_dir: Path | None,
    prepared_trial: PreparedTrialRun,
    worker_id: str | None,
    queue_slot: int | None,
    worker_job_id: str | None = None,
) -> TrialExecutionResult:
    execution_error: str | None = None
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_trial_execution(
            trial_id=trial_id,
            worker_id=worker_id,
            queue_slot=queue_slot,
            stop_event=heartbeat_stop,
            worker_job_id=worker_job_id,
        )
    )
    try:
        try:
            env_type = EnvironmentType(
                (
                    prepared_trial.trial_environment or settings.harbor_environment
                ).lower()
            )
        except ValueError as exc:
            raise ValueError(
                "Invalid harbor environment: "
                f"{prepared_trial.trial_environment or settings.harbor_environment}"
            ) from exc

        outcome = await run_harbor_trial_async(
            task_path=task_path_to_run,
            agent=prepared_trial.trial_agent,
            jobs_dir=Path(settings.harbor_jobs_dir),
            model=prepared_trial.trial_model,
            environment=env_type,
            hook_callback=partial(_handle_harbor_event, trial_id=trial_id),
            trial_id=trial_id,
            harbor_config=prepared_trial.trial_harbor_config,
        )
    except asyncio.CancelledError:
        # CancelledError inherits from BaseException, not Exception, so must be caught explicitly.
        # This can happen if the worker is shutdown mid-trial.
        import traceback

        tb = traceback.format_exc()
        execution_error = (
            "Trial was cancelled by the worker runtime. This typically means the worker "
            "was restarted, hit a timeout, or the job was explicitly cancelled. "
            f"Check worker logs for details.\n\nTraceback:\n{tb}"
        )
        console.print(f"[yellow]Trial {trial_id} cancelled: {execution_error}[/yellow]")
        outcome = None
        # Don't re-raise - we want to properly update the trial status in the database
    except Exception as e:
        execution_error = f"{type(e).__name__}: {e}"
        console.print(f"[red]Trial {trial_id} execution error: {execution_error}[/red]")
        outcome = None
    finally:
        heartbeat_stop.set()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        # Clean up temp task directory
        if temp_task_dir and temp_task_dir.exists():
            shutil.rmtree(temp_task_dir, ignore_errors=True)

    return TrialExecutionResult(outcome=outcome, execution_error=execution_error)


async def run_trial_job(
    trial_id: str,
    queue_key: str,
    *,
    worker_id: str | None = None,
    queue_slot: int | None = None,
    modal_function_call_id: str | None = None,
    worker_job_id: str | None = None,
) -> None:
    """
    Execute a claimed trial.

    1. Prepare trial (set metadata, bump attempts)
    2. Execute Harbor trial
    3. Mark trial as success/failed/retrying
    4. Transition task once all trials complete
    """
    console.print(f"[cyan]Processing trial[/cyan] {trial_id} (queue_key={queue_key})")

    # Check idempotency
    async with _trial_session(trial_id) as (session, trial):
        if not trial:
            raise RuntimeError(f"Trial {trial_id} not found in database")

        console.print(
            f"[dim]Trial {trial_id} current status: {trial.status.value}, agent: {trial.agent}[/dim]"
        )

        if trial.idempotency_key and trial.status in (
            TrialStatus.SUCCESS,
            TrialStatus.FAILED,
        ):
            console.print(
                f"[yellow]Trial {trial_id} already processed (idempotent), skipping[/yellow]"
            )
            return

    prepared_trial = await _prepare_trial_run(
        trial_id=trial_id,
        worker_id=worker_id,
        queue_slot=queue_slot,
        modal_function_call_id=modal_function_call_id,
    )
    if prepared_trial is None:
        return

    # Session is now closed - connection returned to pool

    # Determine task path: download from S3 if needed, or use local path
    temp_task_dir = None
    (
        task_path_to_run,
        temp_task_dir,
        resolved_task_s3_key,
    ) = await resolve_task_directory(
        task_id=prepared_trial.task_id,
        task_s3_key=prepared_trial.task_s3_key,
        task_path=prepared_trial.task_path,
    )
    if temp_task_dir:
        console.print(f"[dim]Downloaded task from S3: {resolved_task_s3_key}[/dim]")
    else:
        console.print(f"[dim]Using local task path: {task_path_to_run}[/dim]")

    # Ensure Harbor scratch directories exist before execution starts.
    os.makedirs(settings.harbor_jobs_dir, exist_ok=True)

    execution = await _execute_trial(
        trial_id=trial_id,
        task_path_to_run=task_path_to_run,
        temp_task_dir=temp_task_dir,
        prepared_trial=prepared_trial,
        worker_id=worker_id,
        queue_slot=queue_slot,
        worker_job_id=worker_job_id,
    )

    # Upload trial results to S3.
    #
    trial_s3_key = None
    should_upload_to_s3 = bool(resolved_task_s3_key)
    if should_upload_to_s3 and execution.outcome and execution.outcome.job_dir:
        try:
            storage = get_storage_client()
            trial_s3_key = await storage.upload_trial_results(
                trial_id, execution.outcome.job_dir
            )
            console.print(f"[dim]Uploaded trial results to S3: {trial_s3_key}[/dim]")
            _cleanup_uploaded_job_dir(execution.outcome.job_dir, trial_id)
        except Exception as e:
            console.print(f"[yellow]Failed to upload trial results to S3: {e}[/yellow]")

    await asyncio.shield(
        _store_trial_results(
            trial_id=trial_id,
            outcome=execution.outcome,
            trial_s3_key=trial_s3_key,
            execution_error=execution.execution_error,
        )
    )
