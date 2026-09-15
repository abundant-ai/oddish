"""Run Harbor from an override ref."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import site
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import AgentConfig, EnvironmentConfig
from harbor.trial.hooks import TrialEvent

from oddish.config import BEDROCK_ENV_VARS, settings, to_anthropic_api_model_id
from oddish.core.harbor_source import harbor_git_requirement
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.schemas import HarborConfig
from oddish.task_timeouts import validate_task_timeout_config
from oddish.worker.probe_overlay import PROBE_HARNESS_DIR
from ._entry import EVENT_SENTINEL
from oddish.workers.agents.claude_code import _pinned_harbor_requirement
from .agent_config import (
    _anthropic_hdo_credential_env,
    _build_routed_agent_config,
    _claude_code_forces_direct_api,
    _gateway_env,
    _is_claude_code_agent,
    _temporary_env,
    _trial_requested_model,
    _trial_uses_openai_provider,
    surfaced_anthropic_env,
)
from .outcome import (
    HarborOutcome,
    _extract_outcome_from_job_result,
)
from .runner import (
    HookCallback,
    _check_local_storage_preflight,
    _format_exception_message,
    _patch_task_toml,
)

_ENTRY_PATH = str(Path(__file__).resolve().parent / "_entry.py")
_CHILD_PYTHON = "3.13"
_CHILD_STREAM_LIMIT = 8 * 1024 * 1024
_PARENT_SITE_PACKAGES_ENV = "ODDISH_PARENT_SITE_PACKAGES"
logger = logging.getLogger(__name__)
_ENVIRONMENT_HARBOR_EXTRAS: dict[EnvironmentType, str] = {
    EnvironmentType.DAYTONA: "daytona",
    EnvironmentType.ARCHIL: "archil",
    EnvironmentType.MODAL: "modal",
    EnvironmentType.E2B: "e2b",
    EnvironmentType.RUNLOOP: "runloop",
    EnvironmentType.GKE: "gke",
    EnvironmentType.NOVITA: "novita",
    EnvironmentType.TENSORLAKE: "tensorlake",
    EnvironmentType.CWSANDBOX: "cwsandbox",
    EnvironmentType.WANDB: "wandb",
    EnvironmentType.ISLO: "islo",
    EnvironmentType.EC2: "ec2",
}


class HarborOverrideImportError(Exception):
    """The override Harbor env could not start."""


def _child_process_env() -> dict[str, str]:
    """Expose parent dependencies after the child imports its override Harbor."""
    parent_site_packages = [
        str(Path(path).resolve())
        for path in site.getsitepackages()
        if Path(path).is_dir()
    ]
    if not parent_site_packages:
        raise HarborOverrideImportError(
            "Ephemeral Harbor child cannot locate parent site-packages."
        )
    env = os.environ.copy()
    env[_PARENT_SITE_PACKAGES_ENV] = os.pathsep.join(parent_site_packages)
    return env


def _claude_code_on_direct_anthropic(agent: str, is_probe: bool) -> bool:
    """Whether this trial's claude-code agent runs against the direct Anthropic API.

    Blanking ``BEDROCK_ENV_VARS`` and rewriting the model id are two halves of
    one routing decision, so both callers below read it from here. Splitting
    them is what let a Bedrock inference-profile id reach api.anthropic.com.
    """
    return "claude-code" in (
        agent or ""
    ).strip().lower() and _claude_code_forces_direct_api(is_probe)


def _child_model_name(*, agent: str, model: str | None, is_probe: bool) -> str | None:
    """Resolve the model id for the transport the child will actually use.

    Bedrock inference-profile ids and direct Anthropic API ids are disjoint
    namespaces: ``global.anthropic.claude-opus-5`` resolves only on Bedrock, and
    ``claude-opus-5`` only on api.anthropic.com. Oddish stores every Claude
    trial under the Bedrock id, so a child moved onto the direct API needs the
    id converted or the very first request 404s. The in-process path rewrites
    under this same predicate in ``_build_agent_config``, and the two must not
    disagree: which dispatch path ran a trial is an Oddish scheduling detail.
    """
    if not _claude_code_on_direct_anthropic(agent, is_probe):
        return model
    return to_anthropic_api_model_id(model)


def _runtime_env_overrides(
    *, agent: str, model: str | None, raw_harbor_config: dict[str, Any], is_probe: bool
) -> dict[str, str]:
    """Build child runtime env overrides."""
    uses_openai = _trial_uses_openai_provider(
        agent=agent, model=model, raw_harbor_config=raw_harbor_config
    )
    _, openai_model = _trial_requested_model(
        agent=agent, model=model, raw_harbor_config=raw_harbor_config
    )
    env: dict[str, str] = {}
    if uses_openai:
        env.update(settings.get_openai_agent_env(model=openai_model))
    if _claude_code_on_direct_anthropic(agent, is_probe):
        env.update({var: "" for var in BEDROCK_ENV_VARS})
    return env


_SUBAGENT_MODEL_KEY = "CLAUDE_CODE_SUBAGENT_MODEL"


def _child_probe_subagent_model(routed: AgentConfig, *, is_probe: bool) -> bool:
    """Whether the child must pin the probe's subagent model itself.

    ``_apply_claude_code_probe_subagent_model`` pins a probe's subagent to the
    agent's own model id. For claude-code that id is the child's decision, so
    the pin has to follow it there -- pinning from the parent's canonical id
    leaves a probe whose main agent and subagent run different models.
    """
    return is_probe and _is_claude_code_agent(routed)


def _child_agent_config(
    routed: AgentConfig, *, raw_harbor_config: dict[str, Any], is_probe: bool
) -> dict[str, Any]:
    """Serialize the child's ``AgentConfig`` with in-process provider routing applied.

    The routing an Anthropic-compatible provider needs -- ``ANTHROPIC_BASE_URL``,
    its auth token, the model id its endpoint serves, and blanked ambient
    platform credentials -- lives in ``agent_config.env``, which *routed* already
    carries from the same builder the in-process path uses. Only ``env`` and
    ``kwargs`` are projected onto the submitted dict: the child resolves ``name``
    and ``import_path`` against its own Harbor, which has none of Oddish's
    wrapper agent classes, and every other submitted field crosses unchanged.

    The shaped env stays the *base* layer of the child's merge, which keeps the
    in-process precedence intact -- a submitted agent env and the worker's
    runtime/extra env still win over these defaults.
    """
    payload = dict(raw_harbor_config.get("agent_config") or {})
    if routed.env:
        env = dict(routed.env)
        # The probe pin is the one writer that sets this key to the agent's own
        # model id; every other writer pins the id its endpoint serves, which
        # differs from the routed id by construction. Drop only that one, so the
        # child can re-pin it from the model it actually runs while an
        # endpoint-pinned value (an ``anthropic-hdo/`` alias, say) still crosses.
        if (
            _child_probe_subagent_model(routed, is_probe=is_probe)
            and env.get(_SUBAGENT_MODEL_KEY) == routed.model_name
        ):
            env.pop(_SUBAGENT_MODEL_KEY)
        payload["env"] = env
    if routed.kwargs:
        payload["kwargs"] = dict(routed.kwargs)
    return payload


def _child_model_id(routed: AgentConfig, *, model: str | None) -> str | None:
    """The model id the child should run.

    The routed builder resolves the spelling each transport needs: a LiteLLM
    harness on an ``anthropic-hdo/`` model becomes ``anthropic/<api-id>``, the
    only form LiteLLM parses, and Gemini flips between the ``gemini/`` and
    ``google/`` prefixes with the harness. Taking the builder's own answer keeps
    that decision in one place.

    claude-code is excluded. Its Bedrock and direct-Anthropic ids are a separate
    decision owned by the child's model normalization, so this returns the
    submitted id and lets that own the field.
    """
    if _is_claude_code_agent(routed):
        return model
    return routed.model_name or model


def _child_extra_agent_env(
    *, model: str | None, extra_agent_env: dict[str, str] | None
) -> dict[str, str]:
    """The env layer the child merges last.

    In process the HDO credential is re-applied after the probe/BYOK merge so it
    wins outright. The child's last layer is this one, so the same credential
    rides here; otherwise a probe or BYOK ``ANTHROPIC_API_KEY`` would overwrite
    it and the trial would authenticate with the wrong key. A gateway-routed
    analysis trial supplies its own Anthropic route, and neither path injects
    the HDO credential over it.
    """
    env = dict(extra_agent_env or {})
    if not _gateway_env(extra_agent_env):
        env.update(_anthropic_hdo_credential_env(model))
    return env


def _build_payload(
    *,
    task_path: Path,
    jobs_dir: Path,
    outcome_path: Path,
    agent: str,
    model: str | None,
    environment_config: EnvironmentConfig | None = None,
    environment: EnvironmentType | None = None,
    raw_harbor_config: dict[str, Any],
    is_probe: bool,
    extra_agent_env: dict[str, str] | None = None,
    environment_build_timeout_multiplier: float | None = None,
) -> dict[str, Any]:
    if environment_config is None:
        environment_config = EnvironmentConfig.model_validate(
            raw_harbor_config.get("environment") or {}
        )
        if environment is not None:
            environment_config.type = environment
        if environment_config.type == EnvironmentType.DAYTONA:
            environment_config.kwargs = DaytonaBackend().harbor_env_kwargs(
                dict(environment_config.kwargs)
            )
    # Both the routed build and the runtime env ask
    # ``_claude_code_forces_direct_api``, which reads ``os.environ``. The
    # in-process runner surfaces the trial's own Anthropic credential there
    # first; without it a worker holding only Bedrock credentials answers the
    # routing question for an HDO trial as if the key did not exist.
    with _temporary_env(
        surfaced_anthropic_env(agent=agent, model=model, agent_env=extra_agent_env)
    ):
        routed = _build_routed_agent_config(
            agent=agent,
            model=model,
            raw_harbor_config=raw_harbor_config,
            is_probe=is_probe,
            probe_oddish_env=extra_agent_env,
        )
        runtime_env = _runtime_env_overrides(
            agent=agent,
            model=model,
            raw_harbor_config=raw_harbor_config,
            is_probe=is_probe,
        )
        # Both halves of the model decision read the same surfaced view: the
        # child's Bedrock/direct choice for claude-code, and the routed
        # builder's spelling for every other agent. Evaluate here, inside the
        # wrapper, so neither is asked under the bare worker environment.
        child_model = _child_model_id(
            routed,
            model=_child_model_name(agent=agent, model=model, is_probe=is_probe),
        )
    return {
        "task_path": str(task_path),
        "jobs_dir": str(jobs_dir),
        "outcome_path": str(outcome_path),
        "agent": agent,
        "model": child_model,
        "environment_config": environment_config.model_dump(mode="json"),
        "agent_config": _child_agent_config(
            routed, raw_harbor_config=raw_harbor_config, is_probe=is_probe
        ),
        "probe_subagent_model": _child_probe_subagent_model(routed, is_probe=is_probe),
        "verifier": raw_harbor_config.get("verifier") or {},
        "artifacts": raw_harbor_config.get("artifacts") or [],
        "timeout_multiplier": raw_harbor_config.get("timeout_multiplier"),
        "agent_timeout_multiplier": raw_harbor_config.get("agent_timeout_multiplier"),
        "verifier_timeout_multiplier": raw_harbor_config.get(
            "verifier_timeout_multiplier"
        ),
        "agent_setup_timeout_multiplier": raw_harbor_config.get(
            "agent_setup_timeout_multiplier"
        ),
        # The runner sizes this to cover a GKE Pod's capacity/pod-ready wait and
        # passes it in; it wins over the raw config so the child JobConfig gets
        # the covering multiplier. Off GKE it is None and the raw value stands.
        "environment_build_timeout_multiplier": (
            environment_build_timeout_multiplier
            if environment_build_timeout_multiplier is not None
            else raw_harbor_config.get("environment_build_timeout_multiplier")
        ),
        "retry": raw_harbor_config.get("retry"),
        "runtime_env": runtime_env,
        "probe_task_dir": str(task_path) if is_probe else None,
        "probe_harness_dir": PROBE_HARNESS_DIR,
        "extra_agent_env": _child_extra_agent_env(
            model=model, extra_agent_env=extra_agent_env
        ),
        "agent_harbor_requirement": _agent_harbor_requirement(
            agent=agent,
            is_probe=is_probe,
            source=raw_harbor_config.get("source"),
            sha=raw_harbor_config.get("resolved_sha"),
        ),
    }


def _agent_harbor_requirement(
    *, agent: str, is_probe: bool, source: str | None, sha: str | None
) -> str | None:
    """Build the Harbor install requirement for probe agents."""
    if not is_probe or (agent or "").strip().lower() != "claude-code":
        return None
    if not source or not sha:
        return None
    return _pinned_harbor_requirement(source, sha)


def _bridge_event(data: dict[str, Any], *, trial_id: str | None) -> SimpleNamespace:
    """Build a parent-side event from child NDJSON."""
    result_data = data.get("result")
    result: SimpleNamespace | None = None
    if result_data:
        verifier_result = None
        vr = result_data.get("verifier_result")
        if vr and vr.get("rewards") is not None:
            verifier_result = SimpleNamespace(rewards=vr["rewards"])
        exception_info = None
        ei = result_data.get("exception_info")
        if ei:
            exception_info = SimpleNamespace(
                exception_type=ei.get("exception_type"),
                exception_message=ei.get("exception_message"),
            )
        result = SimpleNamespace(
            verifier_result=verifier_result, exception_info=exception_info
        )
    event_name = data["event"].lower().replace("_", "-")
    return SimpleNamespace(
        event=TrialEvent(event_name),
        trial_id=data.get("trial_id") or trial_id,
        environment=None,
        environment_provider=data.get("environment_provider"),
        environment_external_id=data.get("environment_external_id"),
        result=result,
    )


def _spawn_args(
    source: str, sha: str, *, environment: EnvironmentType = EnvironmentType.DOCKER
) -> list[str]:
    """Build the child process argv."""
    extra = _ENVIRONMENT_HARBOR_EXTRAS.get(environment)
    return [
        "uv",
        "run",
        "--no-project",
        "--with",
        harbor_git_requirement(source, sha, extras=[extra] if extra else None),
        "--python",
        _CHILD_PYTHON,
        _ENTRY_PATH,
    ]


async def _consume_events(
    stream: asyncio.StreamReader,
    *,
    hook_callback: HookCallback | None,
    trial_id: str | None,
    tail: list[str],
) -> None:
    """Bridge child stdout events to the parent hook."""
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if not line:
            continue
        event: dict[str, Any] | None = None
        if EVENT_SENTINEL in line:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get(EVENT_SENTINEL):
                event = parsed
        if event is not None and hook_callback is not None:
            try:
                await hook_callback(_bridge_event(event, trial_id=trial_id))
            except Exception:
                if (
                    str(event.get("event") or "").lower().replace("_", "-")
                    == "environment-provisioned"
                ):
                    raise
        else:
            tail.append(line)
            del tail[:-50]


async def run_ephemeral_harbor_trial(
    task_path: Path,
    agent: str,
    jobs_dir: Path,
    environment_config: EnvironmentConfig,
    model: str | None = None,
    hook_callback: HookCallback | None = None,
    trial_id: str | None = None,
    harbor_config: dict[str, Any] | None = None,
    extra_agent_env: dict[str, str] | None = None,
    environment_build_timeout_multiplier: float | None = None,
    is_probe: bool = False,
    skip_task_validation: bool = False,
) -> HarborOutcome:
    """Run one trial in an override Harbor child process.

    ``environment_build_timeout_multiplier`` is the runner-sized env-build
    multiplier (GKE Pods need the outer build wait to cover their capacity/
    pod-ready wait); when set it overrides the raw config on the child JobConfig,
    so a GKE override trial gets the same sizing as an in-process one. ``None``
    off GKE leaves the caller's value untouched.
    """
    raw = harbor_config or {}
    hc = HarborConfig.model_validate(raw)
    environment = environment_config.type
    source = hc.source
    sha = hc.resolved_sha
    if not source or not sha:
        return HarborOutcome(
            reward=None,
            error="Ephemeral Harbor run is missing a resolved source/sha.",
            exit_code=-1,
            duration_sec=0.0,
            job_result_path=None,
            job_dir=None,
            exception_type="HarborOverrideImportError",
        )

    if not skip_task_validation:
        validate_task_timeout_config(task_path)

    needs_task_patch = bool(hc.docker_image or hc.mcp_servers)
    preflight_error = _check_local_storage_preflight(
        jobs_dir, include_temp_root=needs_task_patch
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

    task_tmpdir = None
    effective_task_path = task_path
    if needs_task_patch:
        import tempfile

        task_tmpdir = tempfile.TemporaryDirectory(prefix="oddish-task-")
        import shutil

        patched_task = Path(task_tmpdir.name) / task_path.name
        shutil.copytree(task_path, patched_task)
        _patch_task_toml(patched_task, hc)
        effective_task_path = patched_task

    outcome_path = unique_parent / "outcome.json"
    start = time.time()
    tail: list[str] = []
    process: asyncio.subprocess.Process | None = None
    payload_path: Path | None = None
    try:
        try:
            payload = _build_payload(
                task_path=effective_task_path,
                jobs_dir=unique_parent,
                outcome_path=outcome_path,
                agent=agent,
                model=model,
                environment_config=environment_config,
                raw_harbor_config=raw,
                is_probe=is_probe,
                extra_agent_env=extra_agent_env,
                environment_build_timeout_multiplier=environment_build_timeout_multiplier,
            )
        except Exception as exc:  # noqa: BLE001 - any build failure is a trial error
            # Same contract as the in-process runner, which builds its Harbor
            # configs inside the try that owns its cleanup: model canonicalization
            # and AgentConfig validation can both fail, and an unroutable model
            # has to settle as a terminal trial error rather than escape this
            # coroutine as a worker-level execution failure. Returning from
            # inside the try still runs the ``finally`` below, so a failure here
            # cannot strand the patched copy of the task tree.
            return HarborOutcome(
                reward=None,
                error=f"Harbor job execution failed: {_format_exception_message(exc)}",
                exit_code=-1,
                duration_sec=0.0,
                job_result_path=None,
                job_dir=None,
                exception_type=type(exc).__name__,
            )
        payload_path = _write_private_payload(payload)
        child_env = _child_process_env()
        for secret_name in (
            "ODDISH_EC2_SSH_PRIVATE_KEY",
            "ODDISH_EC2_AWS_ACCESS_KEY_ID",
            "ODDISH_EC2_AWS_SECRET_ACCESS_KEY",
            "ODDISH_EC2_AWS_SESSION_TOKEN",
        ):
            child_env.pop(secret_name, None)
        process = await asyncio.create_subprocess_exec(
            *_spawn_args(source, sha, environment=environment),
            str(payload_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_CHILD_STREAM_LIMIT,
            start_new_session=True,
            env=child_env,
        )
        assert process.stdout is not None and process.stderr is not None
        stderr_stream = process.stderr
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            while True:
                chunk = await stderr_stream.readline()
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        await asyncio.gather(
            _consume_events(
                process.stdout,
                hook_callback=hook_callback,
                trial_id=trial_id,
                tail=tail,
            ),
            _drain_stderr(),
        )
        returncode = await process.wait()
        duration = time.time() - start

        outcome = _read_outcome(
            outcome_path=outcome_path,
            unique_parent=unique_parent,
            returncode=returncode,
            duration=duration,
            stderr=b"".join(stderr_chunks).decode("utf-8", "replace"),
            stdout_tail="\n".join(tail),
        )
        return outcome
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            _kill_process_group(process)
        raise
    except Exception:
        if process is not None and process.returncode is None:
            _kill_process_group(process)
            await process.wait()
        raise
    finally:
        if payload_path is not None:
            try:
                payload_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Failed to remove ephemeral Harbor payload %s", payload_path
                )
        if task_tmpdir is not None:
            task_tmpdir.cleanup()


def _write_private_payload(payload: dict[str, Any]) -> Path:
    """Write the child payload outside the uploadable job tree with mode 0600."""
    file_descriptor, raw_path = tempfile.mkstemp(
        prefix="oddish-harbor-payload-", suffix=".json"
    )
    payload_path = Path(raw_path)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w") as payload_file:
            json.dump(payload, payload_file)
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        try:
            payload_path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "Failed to remove incomplete ephemeral Harbor payload %s",
                payload_path,
            )
        raise
    return payload_path


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _read_outcome(
    *,
    outcome_path: Path,
    unique_parent: Path,
    returncode: int,
    duration: float,
    stderr: str,
    stdout_tail: str,
) -> HarborOutcome:
    """Read the child outcome."""
    outcome_data: dict[str, Any] | None = None
    if outcome_path.exists():
        try:
            outcome_data = json.loads(outcome_path.read_text())
        except json.JSONDecodeError:
            outcome_data = None

    if outcome_data is None:
        tail = (stderr or stdout_tail or "").strip()[-1500:]
        return HarborOutcome(
            reward=None,
            error=(
                "Ephemeral Harbor child did not produce an outcome "
                f"(exit={returncode}). {tail}"
            ).strip(),
            exit_code=returncode or -1,
            duration_sec=duration,
            job_result_path=None,
            job_dir=unique_parent,
            exception_type="HarborOverrideImportError",
        )

    job_dir = Path(outcome_data.get("job_dir") or unique_parent)
    job_result_path_str = outcome_data.get("job_result_path")
    job_result_path = Path(job_result_path_str) if job_result_path_str else None

    if job_result_path is not None and job_result_path.exists():
        from harbor.models.job.result import JobResult

        try:
            job_result = JobResult.model_validate_json(job_result_path.read_text())
        except Exception as exc:
            return HarborOutcome(
                reward=None,
                error=f"Ephemeral result.json could not be parsed: {exc}",
                exit_code=returncode or -1,
                duration_sec=duration,
                job_result_path=job_result_path,
                job_dir=job_dir,
                exception_type="HarborOverrideImportError",
            )
        return _extract_outcome_from_job_result(
            job_result=job_result,
            job_result_path=job_result_path,
            job_dir=job_dir,
            duration_sec=duration,
        )

    error = outcome_data.get("error") or (stderr or stdout_tail or "").strip()[-1500:]
    return HarborOutcome(
        reward=None,
        error=error or "Ephemeral Harbor run failed without a result.",
        exit_code=returncode or -1,
        duration_sec=duration,
        job_result_path=None,
        job_dir=job_dir,
        exception_type="HarborOverrideImportError",
    )


__all__ = [
    "HarborOverrideImportError",
    "run_ephemeral_harbor_trial",
]
