from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from typing import Awaitable, Callable, Iterator, TextIO

from harbor import Job, JobConfig  # type: ignore[attr-defined]
from harbor.models.task.config import MCPServerConfig, TaskConfig as HarborTaskConfig
from harbor.models.trial.config import (
    AgentConfig,
    TaskConfig,
)
from harbor.models.environment_type import EnvironmentType
from harbor.trial.hooks import TrialHookEvent
from harbor.models.job.result import JobResult

from oddish.config import settings
from oddish.schemas import HarborConfig
from oddish.task_timeouts import validate_task_timeout_config

HookCallback = Callable[[TrialHookEvent], Awaitable[None]]
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _TeeTextIO:
    """Mirror terminal output to a debug log file."""

    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, data: str) -> int:
        self._primary.write(data)
        cleaned = (
            _ANSI_ESCAPE_RE.sub("", data).replace("\r\n", "\n").replace("\r", "\n")
        )
        if cleaned:
            self._secondary.write(cleaned)
        return len(data)

    def flush(self) -> None:
        self._primary.flush()
        self._secondary.flush()

    def isatty(self) -> bool:
        isatty = getattr(self._primary, "isatty", None)
        return bool(isatty and isatty())

    @property
    def encoding(self) -> str | None:
        return getattr(self._primary, "encoding", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


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


@contextlib.contextmanager
def _capture_modal_output(
    job_dir: Path, environment: EnvironmentType
) -> Iterator[Path | None]:
    """Capture Modal SDK output into a trial-local log file."""
    if environment != EnvironmentType.MODAL:
        yield None
        return

    log_path = job_dir / "modal-output.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with contextlib.ExitStack() as stack:
        log_file = stack.enter_context(log_path.open("a", encoding="utf-8"))
        log_file.write(
            "[oddish] Capturing Modal SDK output for this trial. "
            "Image build failures will usually appear here.\n"
        )
        log_file.flush()

        stack.enter_context(
            contextlib.redirect_stdout(_TeeTextIO(sys.stdout, log_file))  # type: ignore[type-var]
        )
        stack.enter_context(
            contextlib.redirect_stderr(_TeeTextIO(sys.stderr, log_file))  # type: ignore[type-var]
        )

        try:
            import modal
        except Exception as exc:
            log_file.write(
                f"[oddish] Failed to enable modal output capture: {type(exc).__name__}: {exc}\n"
            )
            log_file.flush()
            yield log_path
            return

        output_manager = stack.enter_context(modal.enable_output())
        if hasattr(output_manager, "enable_image_logs"):
            output_manager.enable_image_logs()
        if hasattr(output_manager, "set_timestamps"):
            output_manager.set_timestamps(True)

        yield log_path


def _write_debug_result_json(
    *,
    job_dir: Path,
    duration_sec: float,
    exception_type: str,
    exception_message: str,
    debug_log_path: Path | None = None,
) -> Path:
    """Persist a minimal result.json when Harbor fails before writing one."""
    result_path = job_dir / "result.json"
    payload: dict[str, Any] = {
        "trial_results": [],
        "duration_sec": round(duration_sec, 2),
        "exception_info": {
            "exception_type": exception_type,
            "exception_message": exception_message,
        },
        "debug_artifacts": {},
    }
    if debug_log_path is not None:
        payload["debug_artifacts"]["modal_output_log"] = debug_log_path.name
    result_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result_path


def _maybe_add_modal_debug_hint(error_message: str, debug_log_path: Path | None) -> str:
    """Append a short pointer to the captured Modal debug log."""
    if debug_log_path is None:
        return error_message
    return (
        f"{error_message} Captured Modal SDK output in {debug_log_path.name}; "
        "open the trial logs to inspect the image build failure."
    )


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
            (
                MCPServerConfig.model_validate(s.model_dump())
                if not isinstance(s, MCPServerConfig)
                else s
            )
            for s in hc.mcp_servers
        ]
        changed = True

    if changed:
        config_path.write_text(task_config.model_dump_toml())


def _build_agent_config(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
) -> AgentConfig:
    """Build Harbor's full AgentConfig, preserving rich per-trial fields."""
    raw_agent_config = raw_harbor_config.get("agent_config")
    agent_config = (
        AgentConfig.model_validate(raw_agent_config)
        if isinstance(raw_agent_config, dict)
        else AgentConfig(name=agent, model_name=model)
    )

    # Backward compatibility for rows persisted before Oddish stored full
    # Harbor AgentConfig payloads.
    raw_agent_overrides = raw_harbor_config.get("agent_overrides")
    legacy_overrides = (
        dict(raw_agent_overrides) if isinstance(raw_agent_overrides, dict) else {}
    )

    legacy_env = legacy_overrides.get("env")
    if isinstance(legacy_env, dict):
        agent_config.env = {**legacy_env, **agent_config.env}

    legacy_kwargs = legacy_overrides.get("kwargs")
    if isinstance(legacy_kwargs, dict):
        agent_config.kwargs = {**legacy_kwargs, **agent_config.kwargs}

    if (
        agent_config.override_timeout_sec is None
        and legacy_overrides.get("override_timeout_sec") is not None
    ):
        agent_config.override_timeout_sec = legacy_overrides["override_timeout_sec"]
    if (
        agent_config.override_setup_timeout_sec is None
        and legacy_overrides.get("override_setup_timeout_sec") is not None
    ):
        agent_config.override_setup_timeout_sec = legacy_overrides[
            "override_setup_timeout_sec"
        ]
    if (
        agent_config.max_timeout_sec is None
        and legacy_overrides.get("max_timeout_sec") is not None
    ):
        agent_config.max_timeout_sec = legacy_overrides["max_timeout_sec"]

    if agent_config.import_path is None:
        agent_config.name = agent
    if model is not None:
        agent_config.model_name = model

    return agent_config


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
    task_tags: dict[str, str] | None = None,
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
        harbor_config: Optional dict (serialized HarborConfig + Harbor AgentConfig)
        task_tags: Optional task-level tags (from TaskSubmission.tags)

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
    validate_task_timeout_config(task_path)

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

    agent_config = _build_agent_config(
        agent=agent,
        model=model,
        raw_harbor_config=raw,
    )

    # Inject Bedrock project tag for AWS cost allocation (if configured).
    # Priority: per-submission tags["bedrock_project"] > global setting.
    bedrock_tag = (
        (task_tags or {}).get("bedrock_project")
        or settings.bedrock_project_tag
    )
    if bedrock_tag:
        agent_config.env.setdefault("BEDROCK_PROJECT_TAG", bedrock_tag)

    config = JobConfig(
        tasks=[TaskConfig(path=effective_task_path)],
        agents=[agent_config],
        environment=env_config,
        verifier=hc.verifier,
        artifacts=hc.artifacts,
        jobs_dir=unique_parent,
    )

    # Create Harbor Job
    actual_job_dir = unique_parent
    job = Job(config)
    actual_job_dir = job.job_dir

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
    modal_debug_log_path: Path | None = None

    try:
        with _capture_modal_output(actual_job_dir, environment) as captured_log_path:
            modal_debug_log_path = captured_log_path
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
        outcome = _extract_outcome_from_job_result(
            job_result=job_result,
            job_result_path=job_result_path,
            job_dir=job_dir,
            duration_sec=duration,
        )
        if outcome.error:
            outcome = replace(
                outcome,
                error=_maybe_add_modal_debug_hint(outcome.error, modal_debug_log_path),
            )
        return outcome

    except asyncio.CancelledError:
        duration = time.time() - start
        error_message = (
            "Harbor trial cancelled by the runtime. This usually means the worker "
            "was restarted or the sandbox failed during startup. Check worker logs."
        )
        error_message = _maybe_add_modal_debug_hint(error_message, modal_debug_log_path)
        debug_result_path = _write_debug_result_json(
            job_dir=actual_job_dir,
            duration_sec=duration,
            exception_type="CancelledError",
            exception_message=error_message,
            debug_log_path=modal_debug_log_path,
        )
        return HarborOutcome(
            reward=None,
            error=error_message,
            exit_code=-1,
            duration_sec=duration,
            job_result_path=debug_result_path,
            job_dir=actual_job_dir,
        )
    except Exception as e:
        duration = time.time() - start
        error_message = f"Harbor job execution failed: {type(e).__name__}: {e}"
        error_message = _maybe_add_modal_debug_hint(error_message, modal_debug_log_path)
        debug_result_path = _write_debug_result_json(
            job_dir=actual_job_dir,
            duration_sec=duration,
            exception_type=type(e).__name__,
            exception_message=error_message,
            debug_log_path=modal_debug_log_path,
        )
        return HarborOutcome(
            reward=None,
            error=error_message,
            exit_code=-1,
            duration_sec=duration,
            job_result_path=debug_result_path,
            job_dir=actual_job_dir,
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
