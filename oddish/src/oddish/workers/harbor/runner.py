from __future__ import annotations

import asyncio
import math
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from harbor import Job, JobConfig  # type: ignore[attr-defined]
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import (
    EnvironmentConfig,
    MCPServerConfig,
    TaskConfig as HarborTaskConfig,
)
from harbor.models.trial.config import TaskConfig
from harbor.trial.hooks import TrialHookEvent

from oddish.config import BEDROCK_ENV_VARS, settings
from oddish.runtime.registry import get_backend
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


# Harbor's default task-environment ``build_timeout_sec`` -- the base it
# multiplies. Sizing reads each task's own value; this is only the fallback base
# when a task's task.toml cannot be read. Sourced from Harbor so it cannot drift.
_ENV_BUILD_TIMEOUT_BASE_SEC: float = EnvironmentConfig.model_fields[
    "build_timeout_sec"
].default
# Headroom the GKE outer wait needs beyond the Pod's ready/capacity wait: it
# covers Harbor-side environment construction and keeps the outer cap strictly
# above the inner pod-ready timeout so the more specific inner error surfaces.
_GKE_ENV_BUILD_OVERHEAD_SEC = 300.0


def _sized_environment_build_timeout_multiplier(
    *,
    environment: EnvironmentType,
    environment_build_timeout_multiplier: float | None,
    timeout_multiplier: float | None,
    pod_ready_timeout_sec: int,
    base_sec: float,
) -> float | None:
    """Grow the environment-build timeout multiplier to cover a GKE Pod's
    capacity/pod-ready wait; a no-op for every other environment.

    A GKE Pod can sit Pending through a DWS flex-start capacity wait for up to
    ``pod_ready_timeout_sec`` before it reports ready. Harbor wraps
    ``environment.start()`` in an outer ``wait_for`` of the effective multiplier
    times the task's own ``build_timeout_sec`` (``base_sec``). Sizing against a
    fixed base would fall short whenever a task sets ``build_timeout_sec`` below
    the default, so the multiplier is computed against the task's real base and
    the outer wait clears ``pod_ready_timeout_sec`` plus build overhead
    regardless of how small that base is. Never lower a larger caller value;
    Harbor resolves an unset env-build multiplier to the general
    ``timeout_multiplier``, so that is honoured as the floor too.

    Returns the multiplier to store -- unchanged (possibly ``None``) off GKE.
    """
    if environment != EnvironmentType.GKE:
        return environment_build_timeout_multiplier
    effective_current = (
        environment_build_timeout_multiplier
        if environment_build_timeout_multiplier is not None
        else (timeout_multiplier if timeout_multiplier is not None else 1.0)
    )
    needed = math.ceil((pod_ready_timeout_sec + _GKE_ENV_BUILD_OVERHEAD_SEC) / base_sec)
    return float(max(effective_current, needed))


def _effective_pod_ready_timeout_sec(
    env_kwargs: dict[str, Any], default_sec: int
) -> int:
    """The pod-ready timeout Harbor will actually enforce for a GKE Pod.

    ``GkeBackend.harbor_env_kwargs`` seeds ``pod_ready_timeout_sec`` from the
    platform default but lets a submission override it (caller-wins), and that
    override can arrive as a string via ``--environment-kwarg``. Read the merged
    value, coercing to int, and fall back to ``default_sec`` when it is absent or
    unparseable, so the outer build wait is sized to the timeout the Pod is
    really given rather than the smaller raw setting.
    """
    raw = env_kwargs.get("pod_ready_timeout_sec")
    if raw is None:
        return default_sec
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default_sec


def _effective_task_build_timeout_sec(task_path: Path) -> float:
    """The task's own environment ``build_timeout_sec`` -- the base Harbor
    multiplies by ``environment_build_timeout_multiplier`` for the outer build
    wait.

    Read from the task's task.toml so the GKE outer wait is sized against the
    real base even when a task sets a sub-default ``build_timeout_sec``. Falls
    back to Harbor's ``EnvironmentConfig`` default when the config cannot be read
    or is non-positive -- Harbor would fail such a trial at load anyway, so the
    fallback only needs to keep the sizing arithmetic safe.
    """
    config_path = task_path / "task.toml"
    try:
        task_config = HarborTaskConfig.model_validate_toml(config_path.read_text())
        base = float(task_config.environment.build_timeout_sec)
    except Exception:
        return _ENV_BUILD_TIMEOUT_BASE_SEC
    return base if base > 0 else _ENV_BUILD_TIMEOUT_BASE_SEC


def _read_query_cli_text() -> str:
    """Thin wrapper so tests can monkeypatch without reaching into probe_staging."""
    from oddish.worker.probe_staging import read_query_cli_text

    return read_query_cli_text()


def _probe_modal_kwargs(is_probe: bool, environment: EnvironmentType) -> dict[str, Any]:
    """Return extra env_config.kwargs to inject when running a probe on Modal.

    Passes the oddish-query CLI source to the Modal image so the harbor fork
    can bake it in at instantiation. Returns empty dict for non-probe or
    non-Modal trials so the caller can unconditionally merge.
    """
    if not is_probe or environment != EnvironmentType.MODAL:
        return {}
    return {
        "probe_cli_content": _read_query_cli_text(),
        "probe_cli_path": "/probe-harness/oddish-query",
    }


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


def _assert_tpu_backend(environment, backend, override_tpu) -> None:
    """Fail fast when a TPU-requesting trial is routed to a TPU-less backend.

    Without this, the trial runs WITHOUT the accelerator and dies minutes later
    on its own device asserts -- a confusing failure that looks like a workload
    bug. Only the override_tpu channel is visible here; a TPU declared solely
    in task.toml is parsed by Harbor after this point.
    """
    if override_tpu is None:
        return
    if backend is not None and backend.capabilities().tpu is not None:
        return
    raise RuntimeError(
        f"TPU trials must run on the GKE backend: this trial requests a TPU "
        f"({override_tpu.type}) but is routed to environment "
        f"'{environment.value}', which has no TPU support. Resubmit with "
        f"environment=gke ('oddish run' auto-routes TPU tasks)."
    )


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

    # Size the environment-build timeout multiplier BEFORE the dispatch fork so
    # EVERY path that runs a GKE environment carries it -- the in-process blessed
    # variant AND the out-of-process ephemeral child. pod_ready is read from the
    # raw submission kwargs (the same override channel the GKE backend merges
    # under its platform default), so it matches the value the Pod is actually
    # given; the task's own build_timeout_sec is the base Harbor multiplies. This
    # is a no-op (returns the caller's value, possibly None) off GKE.
    env_build_multiplier = _sized_environment_build_timeout_multiplier(
        environment=environment,
        environment_build_timeout_multiplier=hc.environment_build_timeout_multiplier,
        timeout_multiplier=hc.timeout_multiplier,
        pod_ready_timeout_sec=_effective_pod_ready_timeout_sec(
            hc.environment.kwargs, settings.gke_pod_ready_timeout_sec
        ),
        base_sec=_effective_task_build_timeout_sec(task_path),
    )

    # The TPU gate runs BEFORE the ephemeral early-return so BOTH engines get
    # the fast-fail: an out-of-process trial with override_tpu on a TPU-less
    # backend would otherwise skip it entirely.
    _assert_tpu_backend(
        environment,
        get_backend(environment.value),
        getattr(hc.environment, "override_tpu", None),
    )

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
            environment_build_timeout_multiplier=env_build_multiplier,
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

        backend = get_backend(environment.value)
        if backend is not None:
            env_config.kwargs = backend.harbor_env_kwargs(env_config.kwargs)
        probe_modal = _probe_modal_kwargs(is_probe, environment)
        if probe_modal:
            env_config.kwargs = {**probe_modal, **env_config.kwargs}
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
        # A BYOK user key arrives in the agent env, but claude-code's
        # direct-vs-Bedrock routing reads os.environ -- both to pick the model
        # id (in _build_agent_config) and to blank Bedrock creds (below). Surface
        # the user key as ambient for the build + run so the trial routes to the
        # direct Anthropic API even on a worker with no platform key; the agent
        # then authenticates with the user's key.
        byok_anthropic_env: dict[str, str] = {}
        if "claude-code" in (agent or "").strip().lower():
            _byok_key = (extra_agent_env or {}).get("ANTHROPIC_API_KEY")
            if _byok_key:
                byok_anthropic_env["ANTHROPIC_API_KEY"] = _byok_key

        with _temporary_env(byok_anthropic_env):
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
        # Reuse the multiplier sized before the dispatch fork (identical inputs:
        # pod_ready from the same submission kwargs, base from the same task).
        if env_build_multiplier is not None:
            job_config_kwargs["environment_build_timeout_multiplier"] = (
                env_build_multiplier
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
        # Keep the BYOK key ambient for Job.create/run too, so Harbor's own
        # os.environ-based Bedrock-mode check agrees with the direct model id.
        runtime_env.update(byok_anthropic_env)
        is_claude_code = "claude-code" in (agent or "").strip().lower()
        if is_claude_code and (
            byok_anthropic_env or _claude_code_forces_direct_api(is_probe)
        ):
            # Harbor's _is_bedrock_mode() reads os.environ, and the Modal image
            # bakes in Bedrock credentials. Blank them when claude-code runs
            # against the direct Anthropic API -- platform force-direct, or a
            # BYOK user key.
            runtime_env.update({var: "" for var in BEDROCK_ENV_VARS})
        with _temporary_env(runtime_env):
            job = await Job.create(config)
            actual_job_dir = job.job_dir

            if hook_callback:
                job.on_trial_started(hook_callback)
                job.on_environment_started(hook_callback)
                job.on_agent_started(hook_callback)
                job.on_agent_ended(hook_callback)
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


__all__ = [
    "HarborOutcome",
    "log_local_storage_snapshot",
    "run_harbor_trial_async",
]
