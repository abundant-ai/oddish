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

from harbor import Job, JobConfig  # type: ignore[attr-defined]
from harbor.models.task.config import MCPServerConfig, TaskConfig as HarborTaskConfig
from harbor.models.trial.config import (
    AgentConfig,
    TaskConfig,
)
from harbor.models.environment_type import EnvironmentType
from harbor.trial.hooks import TrialHookEvent
from harbor.models.job.result import JobResult

from oddish.schemas import HarborConfig

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


def _extract_tokens_from_trajectory(
    job_dir: Path,
) -> tuple[int | None, int | None, int | None, float | None]:
    """Fallback: read token counts from ATIF trajectory final_metrics."""
    import json

    if not job_dir or not job_dir.exists():
        return None, None, None, None
    for traj_path in job_dir.rglob("trajectory.json"):
        try:
            data = json.loads(traj_path.read_text())
            fm = data.get("final_metrics")
            if not fm:
                continue
            return (
                fm.get("total_prompt_tokens"),
                fm.get("total_completion_tokens"),
                fm.get("total_cached_tokens"),
                fm.get("total_cost_usd"),
            )
        except Exception:
            continue
    return None, None, None, None


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

    # Fallback: read from ATIF trajectory final_metrics if AgentContext was empty
    if input_tokens is None and output_tokens is None:
        t_in, t_out, t_cache, t_cost = _extract_tokens_from_trajectory(job_dir)
        input_tokens = t_in
        output_tokens = t_out
        cache_tokens = t_cache
        if cost_usd is None:
            cost_usd = t_cost

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


def _patch_task_toml(task_dir: Path, hc: HarborConfig) -> None:
    """Patch task.toml with ``docker_image`` and ``mcp_servers`` from *hc*.

    These fields are read by Harbor from the task's task.toml rather than
    the job/trial config, so we patch the file before execution.
    """
    config_path = task_dir / "task.toml"
    if not config_path.exists():
        return

    try:
        task_config = HarborTaskConfig.model_validate_toml(config_path.read_text())
    except Exception:
        return

    changed = False

    if hc.docker_image:
        task_config.environment.docker_image = str(hc.docker_image)
        changed = True

    if hc.mcp_servers:
        task_config.environment.mcp_servers = [
            MCPServerConfig.model_validate(s.model_dump()) if not isinstance(s, MCPServerConfig) else s
            for s in hc.mcp_servers
        ]
        changed = True

    if changed:
        config_path.write_text(task_config.model_dump_toml())


# =============================================================================
# Harbor Python API Integration (with Hooks)
# =============================================================================


async def run_harbor_trial_async(
    task_path: Path,
    agent: str,
    jobs_dir: Path,
    model: str | None = None,
    environment: EnvironmentType = EnvironmentType.DOCKER,
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
        hook_callback: Optional callback invoked for trial lifecycle events
        trial_id: Optional trial ID for traceability
        harbor_config: Optional dict (serialized HarborConfig + agent_overrides)

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

    raw = harbor_config or {}
    hc = HarborConfig.model_validate(raw)
    agent_overrides: dict[str, Any] = raw.get("agent_overrides", {})

    # ── Task patching ────────────────────────────────────────────────────
    needs_task_patch = bool(hc.docker_image or hc.mcp_servers)
    task_tmpdir: tempfile.TemporaryDirectory | None = None
    effective_task_path = task_path

    if needs_task_patch:
        task_tmpdir = tempfile.TemporaryDirectory(prefix="oddish-task-")
        patched_task = Path(task_tmpdir.name) / task_path.name
        shutil.copytree(task_path, patched_task)
        _patch_task_toml(patched_task, hc)
        effective_task_path = patched_task

    # ── Build Harbor configs ─────────────────────────────────────────────
    env_config = hc.environment.model_copy()
    env_config.type = environment

    agent_config = AgentConfig(
        name=agent,
        model_name=model,
        override_timeout_sec=agent_overrides.get("override_timeout_sec"),
        override_setup_timeout_sec=agent_overrides.get("override_setup_timeout_sec"),
        kwargs=agent_overrides.get("kwargs", {}),
        env=agent_overrides.get("env", {}),
    )

    config = JobConfig(
        tasks=[TaskConfig(path=effective_task_path)],
        agents=[agent_config],
        environment=env_config,
        verifier=hc.verifier,
        artifacts=hc.artifacts,
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

        # Harbor creates job_dir = jobs_dir / job_name (job_name defaults to timestamp).
        job_dir = job.job_dir
        job_result_path = job_dir / "result.json"

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
                hook_callback=hook_callback,
                trial_id=trial_id,
                harbor_config=harbor_config,
            )
        )
    raise RuntimeError("run_harbor_trial cannot be called from an active event loop.")
