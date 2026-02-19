from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
import asyncio
from pathlib import Path
from typing import Callable, Awaitable

from typing import Any

import toml  # type: ignore[import-untyped]

from harbor import Job, JobConfig  # type: ignore[attr-defined]
from harbor.models.trial.config import (
    AgentConfig,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)
from harbor.models.environment_type import EnvironmentType
from harbor.trial.hooks import TrialHookEvent
from harbor.models.job.result import JobResult

HookCallback = Callable[[TrialHookEvent], Awaitable[None]]


@dataclass(frozen=True)
class HarborOutcome:
    """Oddish-specific summary of a Harbor trial execution.

    Not Harbor's TrialResult/JobResult — this flattens the deeply nested Harbor
    result tree into a simple struct that Oddish persists to Postgres and returns
    via its API.  Fields like reward (int 0/1), cost_usd, and phase_timing are
    extracted from Harbor's TrialResult/AgentContext/VerifierResult in
    _extract_outcome_from_job_result().
    """

    reward: int | None  # 0 or 1
    error: str | None
    exit_code: int
    duration_sec: float
    job_result_path: Path | None
    job_dir: Path | None  # Full job directory for S3 upload

    # Token usage & cost (from Harbor's AgentContext)
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    # Per-phase timing breakdown (seconds)
    phase_timing: dict[str, Any] | None = None

    # Whether an ATIF trajectory file exists
    has_trajectory: bool = False


def _extract_timing_info(trial_result: Any) -> dict[str, Any] | None:
    """Extract per-phase timing from a TrialResult's TimingInfo fields."""
    timing: dict[str, Any] = {}
    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        info = getattr(trial_result, phase, None)
        if info and info.started_at and info.finished_at:
            timing[phase] = {
                "started_at": info.started_at.isoformat(),
                "finished_at": info.finished_at.isoformat(),
                "duration_sec": round(
                    (info.finished_at - info.started_at).total_seconds(), 2
                ),
            }
    return timing or None


def _detect_trajectory(job_dir: Path) -> bool:
    """Check if any ATIF trajectory file exists in the job output."""
    if not job_dir or not job_dir.exists():
        return False
    if any(job_dir.rglob("trajectory.json")):
        return True
    if any(job_dir.rglob("trajectory.jsonl")):
        return True
    return False


def _extract_outcome_from_job_result(
    job_result: JobResult,
    job_result_path: Path,
    job_dir: Path,
    duration_sec: float,
) -> HarborOutcome:
    """Extract reward, error, token usage, timing, and trajectory from Harbor's JobResult."""
    # Extract error from trial results
    error: str | None = None
    for trial_result in job_result.trial_results:
        if trial_result.exception_info:
            exc = trial_result.exception_info
            msg = exc.exception_message or exc.exception_type
            if msg:
                error = str(msg)
                break

    # Extract token usage & cost from the first trial's AgentContext
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    phase_timing: dict[str, Any] | None = None

    for trial_result in job_result.trial_results:
        ctx = trial_result.agent_result
        if ctx and not ctx.is_empty():
            input_tokens = ctx.n_input_tokens
            cache_tokens = ctx.n_cache_tokens
            output_tokens = ctx.n_output_tokens
            cost_usd = ctx.cost_usd
            break

    # Extract per-phase timing from the first trial result
    for trial_result in job_result.trial_results:
        phase_timing = _extract_timing_info(trial_result)
        if phase_timing:
            break

    has_trajectory = _detect_trajectory(job_dir)

    def _outcome(reward: int | None) -> HarborOutcome:
        return HarborOutcome(
            reward=reward,
            error=error,
            exit_code=0,
            duration_sec=duration_sec,
            job_result_path=job_result_path,
            job_dir=job_dir,
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            phase_timing=phase_timing,
            has_trajectory=has_trajectory,
        )

    # Method 1: Check reward_stats in job stats
    if job_result.stats.evals:
        first_eval = next(iter(job_result.stats.evals.values()))
        if first_eval.reward_stats and "reward" in first_eval.reward_stats:
            reward_map = first_eval.reward_stats["reward"]
            if 1 in reward_map or 1.0 in reward_map:
                return _outcome(1)
            if 0 in reward_map or 0.0 in reward_map:
                return _outcome(0)

    # Method 2: Check trial results directly
    for trial_result in job_result.trial_results:
        if trial_result.verifier_result and trial_result.verifier_result.rewards:
            reward_value = trial_result.verifier_result.rewards.get("reward")
            if reward_value is not None:
                return _outcome(int(float(reward_value)))

    return _outcome(None)


def _patch_task_toml(task_dir: Path, harbor_config: dict[str, Any]) -> None:
    """Patch task.toml in *task_dir* with overrides from *harbor_config*.

    Handles fields that Harbor reads from the task config rather than
    the job/trial config: ``docker_image``.
    """
    config_path = task_dir / "task.toml"
    if not config_path.exists():
        return

    data = toml.loads(config_path.read_text())
    env_section = data.setdefault("environment", {})
    changed = False

    docker_image = harbor_config.get("docker_image")
    if docker_image:
        env_section["docker_image"] = docker_image
        changed = True

    if changed:
        config_path.write_text(toml.dumps(data))


# =============================================================================
# Harbor Python API Integration (with Hooks)
# =============================================================================


async def run_harbor_trial_async(
    task_path: Path,
    agent: str,
    jobs_dir: Path,
    model: str | None = None,
    environment: EnvironmentType = EnvironmentType.DOCKER,
    timeout_minutes: int = 60,
    hook_callback: HookCallback | None = None,
    trial_id: str | None = None,
    harbor_config: dict[str, Any] | None = None,
) -> HarborOutcome:
    """
    Execute a Harbor trial using Harbor's Python API with lifecycle hooks.

    Args:
        task_path: Path to the Harbor task directory
        agent: Agent name (e.g., "claude-code", "nop", "oracle")
        jobs_dir: Directory for job artifacts
        model: Optional model override
        environment: Execution backend (EnvironmentType)
        timeout_minutes: Timeout for the trial
        hook_callback: Optional callback invoked for trial lifecycle events
        trial_id: Optional trial ID for traceability
        harbor_config: Optional dict with Harbor passthrough config

    Returns:
        HarborOutcome with reward, error, tokens, cost, timing, trajectory, and paths
    """
    # Check disk space before running trial
    disk_usage = shutil.disk_usage(jobs_dir)
    free_gb = disk_usage.free / (1024**3)
    min_required_gb = 5.0

    if free_gb < min_required_gb:
        return HarborOutcome(
            reward=None,
            error=f"Insufficient disk space: {free_gb:.1f}GB free (minimum {min_required_gb}GB required)",
            exit_code=-1,
            duration_sec=0.0,
            job_result_path=None,
            job_dir=None,
        )

    # Create unique job directory
    unique_suffix = trial_id if trial_id else uuid.uuid4().hex[:8]
    unique_parent = jobs_dir / f"{task_path.name}.{agent}.{unique_suffix}"
    unique_parent.mkdir(parents=True, exist_ok=True)

    hc = harbor_config or {}

    # ── Task patching ────────────────────────────────────────────────────
    # docker_image and mcp_servers are read by Harbor from the task's
    # task.toml, not from the job config.  When the caller supplies
    # overrides we copy the task to a temporary directory and patch its
    # task.toml so Harbor picks them up.
    needs_task_patch = bool(hc.get("docker_image"))
    task_tmpdir: tempfile.TemporaryDirectory | None = None
    effective_task_path = task_path

    if needs_task_patch:
        task_tmpdir = tempfile.TemporaryDirectory(prefix="oddish-task-")
        patched_task = Path(task_tmpdir.name) / task_path.name
        shutil.copytree(task_path, patched_task)
        _patch_task_toml(patched_task, hc)
        effective_task_path = patched_task

    # ── Build Harbor EnvironmentConfig ───────────────────────────────────
    env_type = environment
    env_kwargs: dict[str, Any] = {}

    # Network isolation: allow_internet maps to network_block_all for Daytona
    allow_internet = hc.get("allow_internet")
    if env_type == EnvironmentType.DAYTONA:
        if allow_internet is not None:
            env_kwargs["network_block_all"] = not allow_internet
        else:
            env_kwargs["network_block_all"] = False

        # Daytona lifecycle controls
        if hc.get("auto_stop_interval_mins") is not None:
            env_kwargs["auto_stop_interval_mins"] = hc["auto_stop_interval_mins"]
        if hc.get("auto_delete_interval_mins") is not None:
            env_kwargs["auto_delete_interval_mins"] = hc["auto_delete_interval_mins"]
        if hc.get("snapshot_template_name") is not None:
            env_kwargs["snapshot_template_name"] = hc["snapshot_template_name"]

    # Modal sandbox lifecycle controls
    if env_type == EnvironmentType.MODAL:
        if hc.get("sandbox_timeout_secs") is not None:
            env_kwargs["sandbox_timeout_secs"] = hc["sandbox_timeout_secs"]
        if hc.get("sandbox_idle_timeout_secs") is not None:
            env_kwargs["sandbox_idle_timeout_secs"] = hc["sandbox_idle_timeout_secs"]

    # GPU type selection forwarded via kwargs
    gpu_types = hc.get("env_gpu_types")
    if gpu_types:
        env_kwargs["gpu_types"] = gpu_types

    env_config = EnvironmentConfig(
        type=env_type,
        override_cpus=hc.get("env_cpus"),
        override_memory_mb=hc.get("env_memory_mb"),
        override_storage_mb=hc.get("env_storage_mb"),
        override_gpus=hc.get("env_gpus"),
        force_build=hc.get("force_build", False),
        kwargs=env_kwargs,
    )

    # Build Harbor AgentConfig
    agent_kwargs = hc.get("agent_kwargs") or {}
    if hc.get("mcp_servers"):
        agent_kwargs["mcp_servers"] = hc["mcp_servers"]

    agent_env = hc.get("agent_env")
    if agent_env:
        agent_kwargs["env"] = agent_env

    # Respect task.toml agent timeout by default. Only apply an override
    # when API callers explicitly set timeout_minutes.
    agent_timeout_override_sec = hc.get("agent_timeout_sec")

    agent_config = AgentConfig(
        name=agent,
        model_name=model,
        override_timeout_sec=agent_timeout_override_sec,
        override_setup_timeout_sec=hc.get("agent_setup_timeout_sec"),
        kwargs=agent_kwargs,
    )

    # Build Harbor VerifierConfig
    verifier_config = VerifierConfig(
        disable=hc.get("disable_verification", False),
        override_timeout_sec=hc.get("verifier_timeout_sec"),
    )

    config = JobConfig(
        tasks=[TaskConfig(path=effective_task_path)],
        agents=[agent_config],
        environment=env_config,
        verifier=verifier_config,
        jobs_dir=unique_parent,
    )

    # Create Harbor Job
    job = Job(config)

    # Register hooks if callback provided
    if hook_callback:
        # Pass the TrialHookEvent directly to the callback
        job.on_trial_started(hook_callback)
        job.on_environment_started(hook_callback)
        job.on_agent_started(hook_callback)
        job.on_verification_started(hook_callback)
        job.on_trial_ended(hook_callback)
        job.on_trial_cancelled(hook_callback)

    # Run the job
    start = time.time()
    try:
        # Harbor's job.run() returns JobResult object directly
        job_result = await job.run()
        duration = time.time() - start

        # Get paths from job object - Harbor creates job_dir = jobs_dir / job_name
        # job_name defaults to timestamp like "2026-01-15__17-29-55"
        job_dir = job.job_dir
        job_result_path = job._job_result_path

        # Verify paths exist (should always exist after successful run)
        if not job_result_path.exists():
            return HarborOutcome(
                reward=None,
                error="Job result.json not found",
                exit_code=0,
                duration_sec=duration,
                job_result_path=None,
                job_dir=job_dir,
            )

        # Extract reward/error directly from JobResult object (no file parsing needed)
        return _extract_outcome_from_job_result(
            job_result=job_result,
            job_result_path=job_result_path,
            job_dir=job_dir,
            duration_sec=duration,
        )

    except asyncio.CancelledError:
        duration = time.time() - start
        return HarborOutcome(
            reward=None,
            error=(
                "Harbor trial cancelled by the runtime. This usually means the worker "
                "was restarted or the sandbox failed during startup. Check worker logs."
            ),
            exit_code=-1,
            duration_sec=duration,
            job_result_path=None,
            job_dir=unique_parent,
        )
    except Exception as e:
        duration = time.time() - start
        return HarborOutcome(
            reward=None,
            error=f"Harbor job execution failed: {type(e).__name__}: {e}",
            exit_code=-1,
            duration_sec=duration,
            job_result_path=None,
            job_dir=unique_parent,
        )
    finally:
        if task_tmpdir is not None:
            task_tmpdir.cleanup()


def run_harbor_trial(
    task_path: Path,
    agent: str,
    jobs_dir: Path,
    model: str | None = None,
    environment: EnvironmentType = EnvironmentType.DOCKER,
    timeout_minutes: int = 60,
    hook_callback: HookCallback | None = None,
    trial_id: str | None = None,
    harbor_config: dict[str, Any] | None = None,
) -> HarborOutcome:
    """Synchronous wrapper around run_harbor_trial_async."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            run_harbor_trial_async(
                task_path=task_path,
                agent=agent,
                jobs_dir=jobs_dir,
                model=model,
                environment=environment,
                timeout_minutes=timeout_minutes,
                hook_callback=hook_callback,
                trial_id=trial_id,
                harbor_config=harbor_config,
            )
        )
    raise RuntimeError("run_harbor_trial cannot be called from an active event loop.")
