from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

from harbor.models.agent.name import AgentName
from harbor.models.environment_type import EnvironmentType
from harbor.trial.hooks import TrialEvent, TrialHookEvent
from pgqueuer.models import Job

from oddish.config import Settings, settings
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialStatus,
    utcnow,
)
from oddish.db.storage import get_storage_client, resolve_task_directory
from oddish.queue import enqueue_analysis, maybe_start_analysis_stage
from oddish.workers.harbor_runner import run_harbor_trial_async
from oddish.workers.queue.db_helpers import _trial_session
from oddish.workers.queue.shared import console


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
        with open(job_result_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        trial_results = data.get("trial_results") or []
        for trial_result in trial_results:
            if trial_result.get("verifier_result") is not None:
                return True
    except Exception:
        return False
    return False


async def run_trial_job(job: Job, provider: str) -> None:
    """
    Handle a trial job from PGQueuer.

    This is the core trial execution logic:
    1. Mark trial as running
    2. Execute Harbor trial
    3. Mark trial as success/failed/retrying
    4. Mark task completed once trials complete
    """
    payload = json.loads(job.payload.decode())
    trial_id = payload.get("trial_id")

    if not trial_id:
        # Fail the pgqueuer job so it retries and doesn't get marked "success" silently.
        raise ValueError(f"Invalid trial job payload (missing trial_id): {payload}")

    console.print(
        f"[cyan]Processing trial[/cyan] {trial_id} (provider={provider}, pgqueuer_job_id={job.id})"
    )

    # Check idempotency - prevent duplicate processing
    async with _trial_session(trial_id) as (session, trial):
        if not trial:
            # If the trial row isn't visible, retry rather than silently dropping the job.
            raise RuntimeError(f"Trial {trial_id} not found in database")

        console.print(
            f"[dim]Trial {trial_id} current status: {trial.status.value}, agent: {trial.agent}[/dim]"
        )

        # If idempotency key is set and trial is already complete, skip
        if trial.idempotency_key and trial.status in (
            TrialStatus.SUCCESS,
            TrialStatus.FAILED,
        ):
            console.print(
                f"[yellow]Trial {trial_id} already processed (idempotent), skipping[/yellow]"
            )
            return

    # Split session usage - quick DB update, then release connection
    async with _trial_session(trial_id) as (session, trial):
        if not trial:
            console.print(f"[yellow]Trial {trial_id} not found, skipping[/yellow]")
            return

        # Mark as running
        trial.status = TrialStatus.RUNNING
        trial.started_at = utcnow()
        trial.harbor_stage = "starting"  # Initial stage before Harbor events
        trial.attempts += 1

        # Set idempotency key on first attempt
        if not trial.idempotency_key:
            trial.idempotency_key = str(uuid.uuid4())

        # Update task status if needed
        task = await session.get(TaskModel, trial.task_id)
        if task and task.status == TaskStatus.PENDING:
            task.status = TaskStatus.RUNNING
            task.started_at = utcnow()

        # Capture trial details before committing
        task_path = task.task_path if task else None
        task_s3_key = task.task_s3_key if task else None
        task_id = task.id if task else trial.task_id
        trial_agent = trial.agent
        trial_model = trial.model
        if not trial_model and trial_agent in (
            AgentName.NOP.value,
            AgentName.ORACLE.value,
        ):
            trial_model = "default"
            trial.model = trial_model
        trial_environment = trial.environment
        trial_harbor_config = trial.harbor_config

    # Session is now closed - connection returned to pool

    # Determine task path: download from S3 if needed, or use local path
    temp_task_dir = None
    (
        task_path_to_run,
        temp_task_dir,
        resolved_task_s3_key,
    ) = await resolve_task_directory(
        task_id=task_id,
        task_s3_key=task_s3_key,
        task_path=task_path,
    )
    if temp_task_dir:
        console.print(f"[dim]Downloaded task from S3: {resolved_task_s3_key}[/dim]")
    else:
        console.print(f"[dim]Using local task path: {task_path_to_run}[/dim]")

    # Ensure storage directories exist before Harbor uses them
    os.makedirs(settings.harbor_jobs_dir, exist_ok=True)
    os.makedirs(settings.local_storage_dir, exist_ok=True)

    # Run the trial (outside the session context for long-running operation)
    execution_error: str | None = None
    try:
        # Create hook callback for real-time DB updates
        async def on_harbor_event(hook_event: TrialHookEvent) -> None:
            """Update database when Harbor trial lifecycle events occur."""
            event = hook_event.event
            try:
                async with _trial_session(trial_id, allow_missing=True) as (
                    session,
                    trial,
                ):
                    if not trial:
                        return

                    # Log event
                    console.print(f"[dim]Trial {trial_id} event: {event.value}[/dim]")

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
                        suppress_error = False
                        if hook_event.result:
                            result = hook_event.result
                            if (
                                result.verifier_result
                                and result.verifier_result.rewards
                            ):
                                reward_value = result.verifier_result.rewards.get(
                                    "reward"
                                )
                                if reward_value is not None:
                                    extracted_reward = int(float(reward_value))
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
                                        extracted_reward = 0
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

        # Run Harbor trial using Python API
        try:
            env_type = EnvironmentType(
                (trial_environment or Settings.harbor_environment).lower()
            )
        except ValueError as exc:
            raise ValueError(
                f"Invalid harbor environment: {trial_environment or Settings.harbor_environment}"
            ) from exc

        outcome = await run_harbor_trial_async(
            task_path=task_path_to_run,
            agent=trial_agent,
            jobs_dir=Path(settings.harbor_jobs_dir),
            model=trial_model,
            environment=env_type,
            hook_callback=on_harbor_event,
            trial_id=trial_id,
            harbor_config=trial_harbor_config,
        )
    except asyncio.CancelledError:
        # CancelledError inherits from BaseException, not Exception, so must be caught explicitly.
        # This can happen if the worker is shutdown mid-trial or pgqueuer cancels the job.
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
        # Clean up temp task directory
        if temp_task_dir and temp_task_dir.exists():
            shutil.rmtree(temp_task_dir, ignore_errors=True)

    # Upload trial results to S3.
    #
    # NOTE: In some deployments (e.g., Modal) tasks may be stored in S3 even if
    # ODDISH_S3_ENABLED isn't set inside the worker environment. If the task came
    # from S3 (task_s3_key is present), we still want to upload artifacts so the
    # UI can fetch logs/result.json.
    trial_s3_key = None
    should_upload_to_s3 = settings.s3_enabled or bool(resolved_task_s3_key)
    if should_upload_to_s3 and outcome and outcome.job_dir:
        try:
            storage = get_storage_client()
            trial_s3_key = await storage.upload_trial_results(trial_id, outcome.job_dir)
            console.print(f"[dim]Uploaded trial results to S3: {trial_s3_key}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Failed to upload trial results to S3: {e}[/yellow]")

    # New session for storing results (connection was released during Harbor execution)
    async with _trial_session(trial_id, allow_missing=True) as (session, trial):
        if not trial:
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
                    derived_reward = 0
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
                # Harbor produced a test result (0 or 1) - trial executed successfully
                # Hook may have already set status to SUCCESS - that's OK, we're confirming it
                trial.status = TrialStatus.SUCCESS
                trial.finished_at = utcnow()
                console.print(
                    f"[green]Trial {trial_id} SUCCESS[/green] reward={derived_reward}"
                )
            else:
                # No reward - trial encountered an error or didn't complete verification
                # Retry if attempts remain
                if trial.attempts < trial.max_attempts:
                    trial.status = TrialStatus.RETRYING
                    console.print(
                        f"[yellow]Trial {trial_id} retrying ({trial.attempts}/{trial.max_attempts})[/yellow]"
                    )
                    raise Exception(
                        f"Trial failed, retrying: {outcome.error}"
                    )  # PGQueuer will retry
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

        # Immediately enqueue analysis if run_analysis is enabled (don't wait for all trials)
        if trial.status in (TrialStatus.SUCCESS, TrialStatus.FAILED):
            task = await session.get(TaskModel, trial.task_id)
            if task and task.run_analysis and trial.analysis_status is None:
                trial.analysis_status = AnalysisStatus.QUEUED
                await enqueue_analysis(session, trial_id)
                console.print(f"[cyan]Enqueued analysis for {trial_id}[/cyan]")

            # Check if all trials done → transition task status
            started = await maybe_start_analysis_stage(session, trial_id)
            if started:
                console.print(
                    f"[blue]Task {trial.task_id} transitioned to next stage[/blue]"
                )
