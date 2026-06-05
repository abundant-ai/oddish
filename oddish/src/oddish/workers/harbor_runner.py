from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import warnings
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

from oddish.config import OPENAI_PROVIDER_OPENAI, settings, to_bedrock_model_id
from oddish.schemas import HarborConfig
from oddish.task_timeouts import validate_task_timeout_config

HookCallback = Callable[[TrialHookEvent], Awaitable[None]]
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MIN_REQUIRED_FREE_GB = 5.0
_MIN_REQUIRED_FREE_INODES = 1024


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
    via its API.  Fields like reward (float score in [0, 1]), cost_usd, and
    phase_timing are
    extracted from Harbor's TrialResult/AgentContext/VerifierResult in
    _extract_outcome_from_job_result().
    """

    reward: float | None
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

    # The Python exception class name (e.g. "AddTestsDirError",
    # "AgentTimeoutError") that ended this trial, sourced from
    # ``TrialResult.exception_info.exception_type`` when Harbor produced one,
    # or ``type(exc).__name__`` when ``run_harbor_trial_async`` itself caught
    # an exception. Used by ``trial_handler._store_trial_results`` to skip
    # trial-level retries on outcomes Harbor's own RetryConfig already marks
    # as non-retryable (e.g. AddTestsDirError on a dying sandbox); without
    # this, oddish re-queues those trials into fresh sandboxes for hours
    # before exhausting ``max_attempts``.
    exception_type: str | None = None


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


def _format_exception_message(exc: BaseException) -> str:
    """Return a concise exception summary, including ExceptionGroup children."""
    base = f"{type(exc).__name__}: {exc}"
    if not isinstance(exc, BaseExceptionGroup) or not exc.exceptions:
        return base

    child_summaries = [
        f"{type(child).__name__}: {child}" for child in exc.exceptions[:3]
    ]
    if len(exc.exceptions) > 3:
        child_summaries.append(f"+{len(exc.exceptions) - 3} more")
    return f"{base} ({'; '.join(child_summaries)})"


def _storage_probe_paths(jobs_dir: Path, *, include_temp_root: bool) -> list[Path]:
    """Return the local scratch roots Oddish should verify before Harbor runs."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    raw_paths: tuple[Path, ...] = (jobs_dir,)
    if include_temp_root:
        raw_paths = (jobs_dir, Path(tempfile.gettempdir()))
    for raw_path in raw_paths:
        resolved = raw_path.resolve()
        if resolved in seen:
            continue
        candidates.append(resolved)
        seen.add(resolved)
    return candidates


def _probe_storage_root(
    path: Path,
    *,
    min_required_gb: float,
    min_required_inodes: int,
) -> str | None:
    """Check bytes, inode headroom, and writeability for one local root."""
    path.mkdir(parents=True, exist_ok=True)

    disk_usage = shutil.disk_usage(path)
    free_gb = disk_usage.free / (1024**3)
    if free_gb < min_required_gb:
        return (
            f"Insufficient local storage at {path}: {free_gb:.1f}GB free "
            f"(minimum {min_required_gb:.1f}GB required)"
        )

    statvfs = os.statvfs(path)
    # Filesystems that don't expose an inode table (overlayfs, btrfs, many
    # tmpfs mounts, Modal's ephemeral "/tmp") report f_files == 0, which forces
    # f_ffree == f_favail == 0 too. That is the "unlimited inodes" signal, not
    # "0 free", so skip the inode check entirely when there is no table.
    total_inodes = getattr(statvfs, "f_files", None)
    if total_inodes:
        free_inodes = getattr(statvfs, "f_favail", None)
        if free_inodes is None or free_inodes < 0:
            free_inodes = getattr(statvfs, "f_ffree", None)
        if free_inodes is not None and free_inodes < min_required_inodes:
            return (
                f"Insufficient local storage inodes at {path}: {free_inodes} free "
                f"(minimum {min_required_inodes} required)"
            )

    probe_dir = path / f".oddish-preflight-{uuid.uuid4().hex}"
    probe_file = probe_dir / "probe.txt"
    try:
        probe_dir.mkdir()
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        probe_dir.rmdir()
    except OSError as exc:
        shutil.rmtree(probe_dir, ignore_errors=True)
        return f"Local storage probe failed at {path}: {type(exc).__name__}: {exc}"
    return None


def log_local_storage_snapshot(path: str | Path) -> None:
    """Log a one-line disk + inode snapshot for *path* on startup.

    Captured once per process start (API server, standalone worker, Modal
    container) so operators can tell at a glance whether a given container
    is on an inode-tracking filesystem (ext4 shows ``N/M inodes free``)
    versus one that doesn't (overlayfs/tmpfs on Modal shows
    ``inode table unlimited``). Never raises — a startup log line should
    not block the process from coming up.
    """
    try:
        probe_path = Path(path)
        probe_path.mkdir(parents=True, exist_ok=True)
        disk_usage = shutil.disk_usage(probe_path)
        statvfs = os.statvfs(probe_path)
        free_gb = disk_usage.free / (1024**3)
        total_gb = disk_usage.total / (1024**3)
        total_inodes = getattr(statvfs, "f_files", 0) or 0
        free_inodes = getattr(statvfs, "f_favail", None)
        if free_inodes is None or free_inodes < 0:
            free_inodes = getattr(statvfs, "f_ffree", 0) or 0
        if total_inodes:
            inode_desc = f"{free_inodes}/{total_inodes} inodes free"
        else:
            inode_desc = "inode table unlimited (no tracking)"
        print(
            f"[oddish] storage snapshot at {probe_path}: "
            f"{free_gb:.1f}GB/{total_gb:.1f}GB bytes free, {inode_desc}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[oddish] storage snapshot at {path} failed: {type(exc).__name__}: {exc}",
            flush=True,
        )


def _check_local_storage_preflight(
    jobs_dir: Path,
    *,
    include_temp_root: bool,
    min_required_gb: float = _MIN_REQUIRED_FREE_GB,
    min_required_inodes: int = _MIN_REQUIRED_FREE_INODES,
) -> str | None:
    """Return a user-facing error when Harbor scratch space is not viable."""
    for root in _storage_probe_paths(jobs_dir, include_temp_root=include_temp_root):
        try:
            error = _probe_storage_root(
                root,
                min_required_gb=min_required_gb,
                min_required_inodes=min_required_inodes,
            )
        except OSError as exc:
            return (
                f"Local storage preflight failed at {root}: {type(exc).__name__}: {exc}"
            )
        if error is not None:
            return error
    return None


def _extract_outcome_from_job_result(
    job_result: JobResult,
    job_result_path: Path,
    job_dir: Path,
    duration_sec: float,
) -> HarborOutcome:
    """Extract reward, error, token usage, timing, and trajectory from Harbor's JobResult."""
    # Extract error and exception type from trial results
    error: str | None = None
    exception_type: str | None = None
    for trial_result in job_result.trial_results:
        if trial_result.exception_info:
            exc = trial_result.exception_info
            msg = exc.exception_message or exc.exception_type
            if msg:
                error = str(msg)
            if exc.exception_type:
                exception_type = str(exc.exception_type)
            if error or exception_type:
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

    def _outcome(reward: float | None) -> HarborOutcome:
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
            exception_type=exception_type,
        )

    # Method 1: Check reward_stats in job stats.
    # Harbor's AgentDatasetStats.reward_stats is
    # ``dict[str, dict[float | int, list[str]]]`` where the innermost value
    # is the list of trial IDs that produced each reward value. Pick the
    # reward with the most trial IDs (most frequent outcome).
    if job_result.stats.evals:
        first_eval = next(iter(job_result.stats.evals.values()))
        if first_eval.reward_stats and "reward" in first_eval.reward_stats:
            reward_map = first_eval.reward_stats["reward"]
            for reward_key, trial_ids in sorted(
                reward_map.items(),
                key=lambda item: len(item[1]),
                reverse=True,
            ):
                if not trial_ids:
                    continue
                try:
                    return _outcome(float(reward_key))
                except (TypeError, ValueError):
                    continue

    # Method 2: Check trial results directly
    for trial_result in job_result.trial_results:
        if trial_result.verifier_result and trial_result.verifier_result.rewards:
            reward_value = trial_result.verifier_result.rewards.get("reward")
            if reward_value is not None:
                return _outcome(float(reward_value))

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


def _apply_claude_code_openrouter_env(agent_config: AgentConfig) -> None:
    """Apply the env shape Claude Code expects for OpenRouter's Anthropic skin."""
    agent_name = (agent_config.name or "").strip().lower()
    model_name = (agent_config.model_name or "").strip().lower()
    if agent_name != "claude-code" or not model_name.startswith("openrouter/"):
        return

    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${OPENROUTER_API_KEY}")
    env.setdefault("ENABLE_TOOL_SEARCH", "false")

    # Claude Code prioritizes these ambient credentials when present in the
    # Modal image. Blank them so the OpenRouter auth/base-url route wins.
    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env


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

    # Trial rows should already store the runtime model id. Keep this as a
    # defensive guard for legacy rows or rich AgentConfig payloads. Explicit
    # "openrouter/..." ids pass through here and the claude-code agent routes
    # them through OpenRouter instead of the container's default transport.
    agent_config.model_name = to_bedrock_model_id(agent_config.model_name)
    _apply_claude_code_openrouter_env(agent_config)

    if _agent_uses_openai_provider(agent_config):
        if settings.get_openai_provider() == OPENAI_PROVIDER_OPENAI:
            warnings.warn(settings.get_public_openai_warning(), stacklevel=2)
        else:
            agent_config.model_name = settings.resolve_azure_openai_deployment(
                agent_config.model_name
            )

    return agent_config


def _agent_uses_openai_provider(agent_config: AgentConfig) -> bool:
    agent = getattr(agent_config, "name", None)
    if not agent:
        return False
    return (
        settings.get_provider_for_trial(
            agent,
            getattr(agent_config, "model_name", None),
        )
        == "openai"
    )


def _trial_requested_model(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
) -> tuple[str, str | None]:
    raw_agent_config = raw_harbor_config.get("agent_config")
    agent_name = agent
    model_name = model
    if isinstance(raw_agent_config, dict):
        agent_name = str(raw_agent_config.get("name") or agent_name)
        if model_name is None:
            raw_model_name = raw_agent_config.get("model_name")
            model_name = str(raw_model_name) if raw_model_name is not None else None
    return agent_name, model_name


def _trial_uses_openai_provider(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
) -> bool:
    agent_name, model_name = _trial_requested_model(
        agent=agent,
        model=model,
        raw_harbor_config=raw_harbor_config,
    )
    return settings.get_provider_for_trial(agent_name, model_name) == "openai"


@contextlib.contextmanager
def _temporary_env(env: dict[str, str]) -> Iterator[None]:
    old_values = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


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
        harbor_config: Optional dict (serialized HarborConfig + Harbor AgentConfig)

    Returns:
        HarborOutcome with reward, error, tokens, cost, timing, trajectory, and paths
    """
    raw = harbor_config or {}
    hc = HarborConfig.model_validate(raw)
    validate_task_timeout_config(task_path)

    # ── Task patching ────────────────────────────────────────────────────
    needs_task_patch = bool(hc.docker_image or hc.mcp_servers)
    preflight_error = _check_local_storage_preflight(
        jobs_dir,
        include_temp_root=needs_task_patch,
    )
    if preflight_error is not None:
        return HarborOutcome(
            reward=None,
            error=preflight_error,
            exit_code=-1,
            duration_sec=0.0,
            job_result_path=None,
            job_dir=None,
            exception_type="LocalStoragePreflightError",
        )

    # Create unique job directory
    unique_suffix = trial_id if trial_id else uuid.uuid4().hex[:8]
    unique_parent = jobs_dir / f"{task_path.name}.{agent}.{unique_suffix}"
    unique_parent.mkdir(parents=True, exist_ok=True)

    task_tmpdir: tempfile.TemporaryDirectory | None = None
    effective_task_path = task_path

    if needs_task_patch:
        task_tmpdir = tempfile.TemporaryDirectory(prefix="oddish-task-")
        patched_task = Path(task_tmpdir.name) / task_path.name
        shutil.copytree(task_path, patched_task)
        _patch_task_toml(patched_task, hc)
        effective_task_path = patched_task

    # Run the job
    actual_job_dir = unique_parent
    start = time.time()
    modal_debug_log_path: Path | None = None

    try:
        # Build Harbor configs inside the try: _build_agent_config normalizes
        # the model id to a Bedrock-native id and raises on an unmapped Claude
        # model, and Job.create performs task/metric resolution that can fail
        # on transient I/O. Keeping both here turns those failures into a
        # well-formed HarborOutcome instead of a bare exception.
        env_config = hc.environment.model_copy()
        env_config.type = environment

        if environment == EnvironmentType.DAYTONA:
            env_config.kwargs = {
                "auto_stop_interval_mins": settings.daytona_auto_stop_interval_mins,
                "auto_delete_interval_mins": settings.daytona_auto_delete_interval_mins,
                "ephemeral": settings.daytona_ephemeral,
                **env_config.kwargs,
            }
        uses_openai_provider = _trial_uses_openai_provider(
            agent=agent,
            model=model,
            raw_harbor_config=raw,
        )
        _, openai_model = _trial_requested_model(
            agent=agent,
            model=model,
            raw_harbor_config=raw,
        )
        agent_config = _build_agent_config(
            agent=agent,
            model=model,
            raw_harbor_config=raw,
        )

        job_config_kwargs: dict[str, Any] = {
            "tasks": [TaskConfig(path=effective_task_path)],
            "agents": [agent_config],
            "environment": env_config,
            "verifier": hc.verifier,
            "artifacts": hc.artifacts,
            "jobs_dir": unique_parent,
        }
        if hc.timeout_multiplier is not None:
            job_config_kwargs["timeout_multiplier"] = hc.timeout_multiplier
        if hc.agent_timeout_multiplier is not None:
            job_config_kwargs["agent_timeout_multiplier"] = hc.agent_timeout_multiplier
        if hc.verifier_timeout_multiplier is not None:
            job_config_kwargs["verifier_timeout_multiplier"] = (
                hc.verifier_timeout_multiplier
            )
        if hc.agent_setup_timeout_multiplier is not None:
            job_config_kwargs["agent_setup_timeout_multiplier"] = (
                hc.agent_setup_timeout_multiplier
            )
        if hc.environment_build_timeout_multiplier is not None:
            job_config_kwargs["environment_build_timeout_multiplier"] = (
                hc.environment_build_timeout_multiplier
            )
        if hc.retry is not None:
            job_config_kwargs["retry"] = hc.retry

        config = JobConfig(**job_config_kwargs)

        openai_env = (
            settings.get_openai_agent_env(model=openai_model)
            if uses_openai_provider
            else {}
        )
        with _temporary_env(openai_env):
            job = await Job.create(config)
            actual_job_dir = job.job_dir

            if hook_callback:
                job.on_trial_started(hook_callback)
                job.on_environment_started(hook_callback)
                job.on_agent_started(hook_callback)
                job.on_verification_started(hook_callback)
                job.on_trial_ended(hook_callback)
                job.on_trial_cancelled(hook_callback)

            with _capture_modal_output(
                actual_job_dir, environment
            ) as captured_log_path:
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
                exception_type="JobResultMissingError",
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
            exception_type="CancelledError",
        )
    except Exception as e:
        duration = time.time() - start
        error_message = f"Harbor job execution failed: {_format_exception_message(e)}"
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
            exception_type=type(e).__name__,
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
