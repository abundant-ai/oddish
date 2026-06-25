from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from harbor import Job, JobConfig  # type: ignore[attr-defined]
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import MCPServerConfig, TaskConfig as HarborTaskConfig
from harbor.models.trial.config import TaskConfig
from harbor.trial.hooks import TrialHookEvent

from oddish.config import BEDROCK_ENV_VARS, settings
from oddish.schemas import HarborConfig
from oddish.task_timeouts import validate_task_timeout_config
from oddish.worker.probe_staging import stage_org_skills
from .agent_config import (
    _build_agent_config,
    _claude_code_forces_direct_api,
    _temporary_env,
    _trial_requested_model,
    _trial_uses_openai_provider,
)
from .modal_debug import (
    _capture_modal_output,
    _format_exception_message,
    _maybe_add_modal_debug_hint,
    _write_debug_result_json,
)
from .outcome import (
    HarborOutcome,
    _extract_outcome_from_job_result,
)
from .patches import apply_harbor_patches
from .storage import (
    _MIN_REQUIRED_FREE_GB,
    _MIN_REQUIRED_FREE_INODES,
    _probe_storage_root,
    _storage_probe_paths,
    log_local_storage_snapshot,
)

HookCallback = Callable[[TrialHookEvent], Awaitable[None]]


def _check_local_storage_preflight(
    jobs_dir: Path,
    *,
    include_temp_root: bool,
    min_required_gb: float = _MIN_REQUIRED_FREE_GB,
    min_required_inodes: int = _MIN_REQUIRED_FREE_INODES,
) -> str | None:
    """Return a user-facing error when Harbor scratch space is not viable.

    Kept as a local wrapper so tests and callers that monkeypatch
    ``harbor_runner._probe_storage_root`` still affect this facade.
    """
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
    extra_agent_env: dict[str, str] | None = None,
) -> HarborOutcome:
    """
    Execute a Harbor trial using Harbor's Python API with lifecycle hooks.

    ``extra_agent_env`` is merged into the built AgentConfig env (probe trials
    use this for the minted read-only oddish CLI creds). It is never persisted.

    Returns a HarborOutcome with reward, error, tokens, cost, timing,
    trajectory presence, and artifact paths.
    """
    apply_harbor_patches()

    raw = harbor_config or {}
    hc = HarborConfig.model_validate(raw)

    # An allowlisted override that is neither the locked default nor a blessed
    # image variant runs out-of-process against its own Harbor: a different
    # Harbor than the one baked into this container cannot be swapped in-process
    # (sys.modules caches it), so route to the child-interpreter engine.
    if hc.variant_id == "ephemeral":
        from .ephemeral import run_ephemeral_harbor_trial

        return await run_ephemeral_harbor_trial(
            task_path=task_path,
            agent=agent,
            jobs_dir=jobs_dir,
            model=model,
            environment=environment,
            hook_callback=hook_callback,
            trial_id=trial_id,
            harbor_config=harbor_config,
            org_id=org_id,
            extra_agent_env=extra_agent_env,
        )

    # Probes attach to an existing task and inherit its task.toml, which may
    # predate the timeout requirement. Rather than hard-fail, skip strict
    # validation and hand the probe a capped default agent timeout below.
    is_probe = raw.get("mode") == "probe"
    if not is_probe:
        validate_task_timeout_config(task_path)

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

    actual_job_dir = unique_parent
    start = time.time()
    modal_debug_log_path: Path | None = None

    try:
        # Build Harbor configs inside the try: model normalization and
        # Job.create can both fail and should return a well-formed outcome.
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
            probe_oddish_env=extra_agent_env,
        )

        # Stage the org's shared skills (+ global seeds) into a root under the
        # job dir and hand it to Harbor via ``AgentConfig.skills``. Best-effort;
        # failure never blocks a trial run.
        if org_id is not None:
            skills_root = unique_parent / "agent_skills"
            skill_ids = raw.get("skill_ids")
            n_skills = await stage_org_skills(
                skills_root, org_id=org_id, skill_ids=skill_ids
            )
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
        is_claude_code = "claude-code" in (agent or "").strip().lower()
        if is_claude_code and _claude_code_forces_direct_api(is_probe):
            # Harbor's _is_bedrock_mode() reads os.environ, and the Modal image
            # bakes in Bedrock credentials. Blank them for this run when
            # claude-code is forced to the direct Anthropic API.
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
                job_result = await job.run()
        duration = time.time() - start

        job_dir = job.job_dir
        job_result_path = job_dir / "result.json"

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
    org_id: str | None = None,
    extra_agent_env: dict[str, str] | None = None,
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
                extra_agent_env=extra_agent_env,
            )
        )
    raise RuntimeError("run_harbor_trial cannot be called from an active event loop.")


__all__ = [
    "HarborOutcome",
    "log_local_storage_snapshot",
    "run_harbor_trial",
    "run_harbor_trial_async",
]
