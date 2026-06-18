from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
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

from oddish.config import (
    BEDROCK_ENV_VARS,
    MINIMAX_DEFAULT_BASE_URL,
    MOONSHOT_DEFAULT_BASE_URL,
    OPENAI_PROVIDER_AZURE,
    OPENAI_PROVIDER_OPENAI,
    ZAI_DEFAULT_BASE_URL,
    is_minimax_model,
    is_moonshot_model,
    is_subscription_model,
    is_zai_model,
    minimax_api_model_id,
    minimax_bare_model_id,
    moonshot_bare_model_id,
    settings,
    subscription_bare_model_id,
    to_anthropic_api_model_id,
    to_bedrock_model_id,
    to_minimax_model_id,
    to_moonshot_model_id,
    to_zai_model_id,
    zai_bare_model_id,
)
from oddish.schemas import HarborConfig
from oddish.worker.probe_staging import stage_org_skills
from oddish.task_timeouts import (
    PROBE_AGENT_TIMEOUT_SEC,
    validate_task_timeout_config,
)

logger = logging.getLogger(__name__)

HookCallback = Callable[[TrialHookEvent], Awaitable[None]]
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MIN_REQUIRED_FREE_GB = 5.0
_MIN_REQUIRED_FREE_INODES = 1024
_ODDISH_CODEX_IMPORT_PATH = "oddish.workers.codex_agent:OddishCodex"
_AZURE_COMPAT_CODEX_IMPORT_PATH = "oddish.workers.codex_agent:AzureCompatibleCodex"
_ODDISH_CLAUDE_CODE_IMPORT_PATH = "oddish.workers.claude_code_agent:OddishClaudeCode"


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


# z.ai recommends these long-context / streaming settings for GLM under Claude
# Code. They are applied with setdefault so a sweep can override any of them.
_ZAI_RECOMMENDED_ENV: dict[str, str] = {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
    "API_TIMEOUT_MS": "3600000",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "3600000",
    "CLAUDE_CODE_EAGER_FLUSH": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
}


def _apply_claude_code_zai_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to z.ai's GLM endpoint.

    GLM is served over an Anthropic-compatible ``/messages`` API, so it runs on
    the claude-code harness but must hit z.ai instead of the Bedrock route the
    Modal image defaults to. Mirrors ``_apply_claude_code_openrouter_env``:
    point Claude Code at the z.ai base URL, authenticate with ``${ZAI_API_KEY}``
    (resolved by Harbor's Modal env at exec time, same as OpenRouter), blank the
    ambient Bedrock/Anthropic credentials so the z.ai route wins, and pin the
    model plus every size alias to the bare GLM id (the image runs in Bedrock
    mode by default, so Claude Code would otherwise not set the aliases).
    """
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    if not is_zai_model(agent_config.model_name):
        return

    bare_model = zai_bare_model_id(agent_config.model_name or "")
    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("ZAI_BASE_URL") or ZAI_DEFAULT_BASE_URL,
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${ZAI_API_KEY}")
    env.setdefault("ENABLE_TOOL_SEARCH", "false")

    # Pin the GLM id for the primary model and all size aliases that Claude Code
    # may route to (Haiku/Sonnet/Opus/subagent), so a single account serves all.
    if bare_model:
        env["ANTHROPIC_MODEL"] = bare_model
        for alias in (
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.setdefault(alias, bare_model)

    for key, value in _ZAI_RECOMMENDED_ENV.items():
        env.setdefault(key, value)

    # The Modal image bakes in the Bedrock route; blank those ambient creds so
    # the z.ai base-url/auth-token route wins.
    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env

    # z.ai recommends "max effort" with adaptive thinking. Harbor's claude-code
    # agent renders these kwargs as `--effort max --thinking adaptive`. Set as
    # defaults so a plain GLM run matches z.ai's recommended setup; a sweep can
    # override either via agent kwargs.
    kwargs = dict(agent_config.kwargs or {})
    kwargs.setdefault("thinking", "adaptive")
    kwargs.setdefault("reasoning_effort", "max")
    agent_config.kwargs = kwargs


def _apply_claude_code_subscription_env(agent_config: AgentConfig) -> None:
    """Authenticate claude-code via a personal Claude subscription OAuth token.

    Mirrors ``_apply_claude_code_zai_env`` but for the official Anthropic
    endpoint: set ``CLAUDE_CODE_OAUTH_TOKEN`` (the long-lived token from
    ``claude setup-token``, resolved from the worker env / Modal secret at exec
    time) and blank the ambient Bedrock + API-key credentials the Modal image
    bakes in. ``ANTHROPIC_API_KEY`` in particular OVERRIDES the subscription in
    non-interactive (``-p``) mode, so it must be blank. The model id is already
    the bare subscription id; Harbor's claude-code agent sends it as
    ``ANTHROPIC_MODEL``.

    NOTE: defeating Bedrock *mode* also requires blanking
    ``CLAUDE_CODE_USE_BEDROCK`` / ``AWS_BEARER_TOKEN_BEDROCK`` in the worker
    PROCESS env (done in ``run_harbor_trial_async``), because Harbor's
    ``_is_bedrock_mode()`` reads ``os.environ``, not the agent env.
    """
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    env = dict(agent_config.env or {})
    env.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "${CS_CLAUDE_CODE_OAUTH_TOKEN}")
    # Blank the ambient creds so the OAuth subscription route wins.
    env["ANTHROPIC_API_KEY"] = ""
    env["ANTHROPIC_AUTH_TOKEN"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    # A stray ANTHROPIC_BASE_URL (like ANTHROPIC_AUTH_TOKEN above) routes the
    # OAuth token at the wrong endpoint and 401s; blank it so Claude Code uses
    # the official Anthropic endpoint.
    env["ANTHROPIC_BASE_URL"] = ""
    agent_config.env = env


def _materialize_codex_auth_json(
    agent_config: AgentConfig,
) -> tempfile.TemporaryDirectory | None:
    """Stage the operator's ChatGPT ``auth.json`` for Harbor's codex agent.

    Harbor authenticates codex from a file on the *worker* (``CODEX_AUTH_JSON_PATH``
    / ``CODEX_FORCE_AUTH_JSON``), then uploads it into the sandbox's
    ``$CODEX_HOME``. Modal delivers secrets as env vars, not files, so this
    decodes ``CS_CODEX_AUTH_JSON_B64`` (base64 of a ``codex login`` auth.json,
    stored in the Modal runtime secret) to a worker-local temp file and points
    ``CODEX_AUTH_JSON_PATH`` at it.

    The file is written to the system temp dir -- deliberately OUTSIDE the Harbor
    job dir -- so the credential is never swept up into the trial's S3 upload.
    Returns the temp dir so the caller can clean it after the run, or ``None``
    when nothing was staged (the caller already pointed at a local file, or no
    secret was provided).
    """
    env = dict(agent_config.env or {})
    if env.get("CODEX_AUTH_JSON_PATH") or env.get("CODEX_FORCE_AUTH_JSON"):
        # Operator supplied an explicit path / force flag (e.g. a local worker
        # with ~/.codex/auth.json). Respect it; nothing to materialize.
        return None
    b64 = os.environ.get("CS_CODEX_AUTH_JSON_B64")
    if not b64:
        raise RuntimeError(
            "codex subscription route is enabled but CS_CODEX_AUTH_JSON_B64 is not "
            "set. Provide the codex auth.json (base64 of a `codex login` auth.json) "
            "via the bring-your-own-creds Modal secret (ODDISH_EXTRA_SECRET_NAME), "
            "or remove codex from ODDISH_SUBSCRIPTION_AGENTS."
        )
    tmp = tempfile.TemporaryDirectory(prefix="oddish-codex-auth-")
    try:
        raw = base64.b64decode(b64)
        auth_path = Path(tmp.name) / "auth.json"
        auth_path.write_bytes(raw)
        auth_path.chmod(0o600)
    except Exception:
        logger.exception(
            "codex subscription route: failed to materialize auth.json"
        )
        tmp.cleanup()
        return None
    # Best-effort freshness probe. codex refreshes its token near ~8 days old;
    # the per-trial re-seed discards refreshed tokens, so a refresh consumes the
    # single-use refresh token and breaks later codex trials. Warn so the
    # operator reseeds a fresh auth.json before a long run. Never block the run.
    try:
        import json
        from datetime import datetime, timezone
        last_refresh = json.loads(raw).get("last_refresh")
        if isinstance(last_refresh, str) and last_refresh:
            ts = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
            if age_days > 7:
                logger.warning(
                    "codex subscription auth.json is %.1f days old "
                    "(last_refresh=%s); codex refreshes near ~8 days and the "
                    "per-trial re-seed discards refreshed tokens, so a refresh can "
                    "break subsequent codex trials. Reseed CS_CODEX_AUTH_JSON_B64 "
                    "with a fresh `codex login` auth.json before a long run.",
                    age_days, last_refresh,
                )
    except Exception:
        pass
    env["CODEX_AUTH_JSON_PATH"] = str(auth_path)
    agent_config.env = env
    return tmp


# MiniMax's recommended long-context / streaming env for M-series models under
# Claude Code. CLAUDE_CODE_AUTO_COMPACT_WINDOW matches MiniMax-M3's 512K window.
_MINIMAX_RECOMMENDED_ENV: dict[str, str] = {
    "API_TIMEOUT_MS": "3000000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "512000",
}

# Moonshot's recommended env for Kimi K2.7 Code under Claude Code.
# CLAUDE_CODE_AUTO_COMPACT_WINDOW matches K2.7's 256K window;
# CLAUDE_CODE_MAX_OUTPUT_TOKENS is K2.7's max output. K2.7 locks
# temperature/top_p server-side and thinking is always on, so no sampling or
# thinking kwargs are set.
_MOONSHOT_RECOMMENDED_ENV: dict[str, str] = {
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "262144",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32768",
}


def _apply_claude_code_minimax_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to MiniMax's direct endpoint.

    Mirrors ``_apply_claude_code_zai_env``: point Claude Code at MiniMax's
    Anthropic-compatible base URL, authenticate with ``${MINIMAX_API_KEY}``,
    blank the ambient Bedrock/Anthropic credentials so the MiniMax route wins,
    and pin the model (re-cased to MiniMax's published id) plus every size alias.
    MiniMax M3 enables extended thinking by default, so no thinking/effort kwargs
    are set.
    """
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    if not is_minimax_model(agent_config.model_name):
        return

    bare_model = minimax_api_model_id(
        minimax_bare_model_id(agent_config.model_name or "")
    )
    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("MINIMAX_BASE_URL") or MINIMAX_DEFAULT_BASE_URL,
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${MINIMAX_API_KEY}")

    if bare_model:
        env["ANTHROPIC_MODEL"] = bare_model
        for alias in (
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.setdefault(alias, bare_model)

    for key, value in _MINIMAX_RECOMMENDED_ENV.items():
        env.setdefault(key, value)

    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env


def _apply_claude_code_moonshot_env(agent_config: AgentConfig) -> None:
    """Apply the env Claude Code needs to talk to Moonshot's Kimi endpoint.

    Mirrors ``_apply_claude_code_zai_env``: point Claude Code at Moonshot's
    Anthropic-compatible base URL, authenticate with ``${MOONSHOT_API_KEY}``,
    blank the ambient Bedrock/Anthropic credentials so the Moonshot route wins,
    and pin the model plus every size alias to the bare Kimi id. K2.7 locks
    sampling params and thinking is always on, so no kwargs are set.
    """
    agent_name = (agent_config.name or "").strip().lower()
    if "claude-code" not in agent_name:
        return
    if not is_moonshot_model(agent_config.model_name):
        return

    bare_model = moonshot_bare_model_id(agent_config.model_name or "")
    env = dict(agent_config.env or {})
    env.setdefault(
        "ANTHROPIC_BASE_URL",
        os.environ.get("MOONSHOT_BASE_URL") or MOONSHOT_DEFAULT_BASE_URL,
    )
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "${MOONSHOT_API_KEY}")

    if bare_model:
        env["ANTHROPIC_MODEL"] = bare_model
        for alias in (
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.setdefault(alias, bare_model)

    for key, value in _MOONSHOT_RECOMMENDED_ENV.items():
        env.setdefault(key, value)

    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CODE_USE_BEDROCK"] = ""
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    agent_config.env = env


def _apply_codex_azure_compat(agent_config: AgentConfig) -> None:
    """Route Azure Codex trials through Oddish's transport-compatible wrapper."""
    if agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "codex":
        return
    if settings.get_openai_provider() != OPENAI_PROVIDER_AZURE:
        return

    agent_config.name = None
    agent_config.import_path = _AZURE_COMPAT_CODEX_IMPORT_PATH


def _apply_codex_oddish_wrapper(agent_config: AgentConfig) -> None:
    """Route Codex trials through Oddish's compatibility wrapper."""
    if agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "codex":
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_CODEX_IMPORT_PATH


def _apply_claude_code_probe_harbor(agent_config: AgentConfig, is_probe: bool) -> None:
    """Install the harbor package in the sandbox for probe claude-code trials."""
    if not is_probe or agent_config.import_path is not None:
        return
    agent_name = (agent_config.name or "").strip().lower()
    if agent_name != "claude-code":
        return

    agent_config.name = None
    agent_config.import_path = _ODDISH_CLAUDE_CODE_IMPORT_PATH


def _agent_uses_bedrock() -> bool:
    """Mirror Harbor's claude-code Bedrock-mode detection.

    Harbor's installed claude-code agent decides Bedrock vs the direct Anthropic
    API purely from the worker process environment (``_is_bedrock_mode`` reads
    ``os.environ``), independent of the model id Oddish hands it. We read the same
    signals so the model id we emit stays consistent with the transport Harbor
    will actually use. The Modal worker image sets ``CLAUDE_CODE_USE_BEDROCK=1``,
    so cloud runs take the Bedrock branch; absent it, the agent falls back to
    ``ANTHROPIC_API_KEY``.
    """
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip() == "1":
        return True
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        return True
    return False


def _build_agent_config(
    *,
    agent: str,
    model: str | None,
    raw_harbor_config: dict[str, Any],
    is_probe: bool = False,
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

    # Probe trials inherit an existing task's task.toml, which may carry a
    # multi-hour agent timeout (or none at all). Cap them at the probe default
    # unless the trial explicitly set its own override above.
    if is_probe and agent_config.override_timeout_sec is None:
        agent_config.override_timeout_sec = PROBE_AGENT_TIMEOUT_SEC

    if agent_config.import_path is None:
        agent_config.name = agent
    if model is not None:
        agent_config.model_name = model

    # Trial rows should already store the runtime model id. Keep this as a
    # defensive guard for legacy rows or rich AgentConfig payloads. Explicit
    # "openrouter/..." ids pass through here and the claude-code agent routes
    # them through OpenRouter instead of the container's default transport.
    # GLM/z.ai, MiniMax, and Moonshot/Kimi models canonicalize to
    # "<provider>/<id>" (kept off the Bedrock route); everything else flows
    # through the Bedrock chokepoint as before. Keeping the provider prefix on
    # model_name lets Harbor's per-agent network allowlist resolve the direct
    # endpoint for closed-internet tasks.
    sub_route = is_subscription_model(agent_config.model_name) or (
        settings._is_subscription_agent(agent)
    )
    if sub_route:
        # Personal-subscription route: use the standard/bare id the agent CLI
        # expects (strips a "sub/" prefix if present; a standard id passes
        # through unchanged), and keep it off the Bedrock chokepoint.
        agent_config.model_name = subscription_bare_model_id(
            agent_config.model_name or ""
        )
    elif is_zai_model(agent_config.model_name):
        agent_config.model_name = to_zai_model_id(agent_config.model_name)
    elif is_minimax_model(agent_config.model_name):
        agent_config.model_name = to_minimax_model_id(agent_config.model_name)
    elif is_moonshot_model(agent_config.model_name):
        agent_config.model_name = to_moonshot_model_id(agent_config.model_name)
    elif _agent_uses_bedrock():
        agent_config.model_name = to_bedrock_model_id(agent_config.model_name)
    else:
        # No Bedrock env: Harbor's claude-code agent authenticates against the
        # direct Anthropic API, which rejects a Bedrock inference-profile id
        # ("global.anthropic.*") with HTTP 400 "Operation not allowed". Hand it
        # the matching plain Anthropic API id instead.
        agent_config.model_name = to_anthropic_api_model_id(agent_config.model_name)
    _apply_claude_code_openrouter_env(agent_config)
    _apply_claude_code_zai_env(agent_config)
    if sub_route:
        # claude-code -> OAuth token env; codex auth.json is staged later in
        # run_harbor_trial_async (it needs a temp file with a managed lifecycle).
        _apply_claude_code_subscription_env(agent_config)
    _apply_claude_code_minimax_env(agent_config)
    _apply_claude_code_moonshot_env(agent_config)

    # Subscription trials never use the Azure/OpenAI credential path -- codex
    # authenticates from the staged auth.json, not OPENAI_API_KEY -- so skip the
    # provider env injection that would otherwise require Azure config.
    if not sub_route and _agent_uses_openai_provider(agent_config):
        if settings.get_openai_provider() == OPENAI_PROVIDER_OPENAI:
            warnings.warn(settings.get_public_openai_warning(), stacklevel=2)
        else:
            agent_config.model_name = settings.resolve_azure_openai_deployment(
                agent_config.model_name
            )
            _apply_codex_azure_compat(agent_config)

    _apply_codex_oddish_wrapper(agent_config)
    _apply_claude_code_probe_harbor(agent_config, is_probe)

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
    org_id: str | None = None,
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

    # Probes attach to an existing task and inherit its task.toml, which may
    # predate the timeout requirement. Rather than hard-fail (the failure that
    # broke probes in prod), skip strict validation and hand the probe a capped
    # default agent timeout below -- mirroring ``worker.local_runner``, which
    # never validated and already applies the same cap.
    is_probe = raw.get("mode") == "probe"
    if not is_probe:
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
    codex_auth_tmpdir: tempfile.TemporaryDirectory | None = None
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
            is_probe=is_probe,
        )

        # Personal-subscription auth route (Claude Code OAuth / Codex auth.json).
        sub_route = (
            is_subscription_model(model)
            or is_subscription_model(openai_model)
            or settings._is_subscription_agent(agent)
        )
        if sub_route and (agent or "").strip().lower() == "codex":
            codex_auth_tmpdir = _materialize_codex_auth_json(agent_config)

        # Stage the org's shared skills (+ global seeds) into a root under the
        # job dir and hand it to Harbor via ``AgentConfig.skills``: Harbor uploads
        # each ``<name>/`` skill into the sandbox and the claude-code agent
        # registers them so the agent discovers them. Best-effort; never blocks.
        if org_id is not None:
            skills_root = unique_parent / "agent_skills"
            n_skills = await stage_org_skills(skills_root, org_id=org_id)
            if n_skills:
                agent_config.skills = [*agent_config.skills, skills_root]

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
        runtime_env = dict(openai_env)
        if sub_route and "claude-code" in (agent or "").strip().lower():
            # Harbor's _is_bedrock_mode() reads os.environ, and the Modal image
            # bakes in CLAUDE_CODE_USE_BEDROCK=1 + AWS_BEARER_TOKEN_BEDROCK.
            # Blank them for this run so claude-code uses the OAuth token instead
            # of Bedrock. _temporary_env restores them afterward.
            runtime_env.update({var: "" for var in BEDROCK_ENV_VARS})
        with _temporary_env(runtime_env):
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
        if codex_auth_tmpdir is not None:
            codex_auth_tmpdir.cleanup()


def run_harbor_trial(
    task_path: Path,
    agent: str,
    jobs_dir: Path,
    model: str | None = None,
    environment: EnvironmentType = EnvironmentType.DOCKER,
    hook_callback: HookCallback | None = None,
    trial_id: str | None = None,
    harbor_config: dict[str, Any] | None = None,
    org_id: str | None = None,
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
                org_id=org_id,
            )
        )
    raise RuntimeError("run_harbor_trial cannot be called from an active event loop.")
