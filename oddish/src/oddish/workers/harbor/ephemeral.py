"""Run Harbor from an override ref."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harbor.models.environment_type import EnvironmentType
from harbor.trial.hooks import TrialEvent

from oddish.config import BEDROCK_ENV_VARS, settings
from oddish.core.harbor_source import harbor_git_requirement
from oddish.schemas import HarborConfig
from oddish.task_timeouts import validate_task_timeout_config
from oddish.worker.probe_overlay import PROBE_HARNESS_DIR
from ._entry import EVENT_SENTINEL
from oddish.workers.agents.claude_code import _pinned_harbor_requirement
from .agent_config import (
    _claude_code_forces_direct_api,
    _trial_requested_model,
    _trial_uses_openai_provider,
)
from .outcome import (
    HarborOutcome,
    _extract_outcome_from_job_result,
)
from .runner import (
    HookCallback,
    _check_local_storage_preflight,
    _patch_task_toml,
)

_ENTRY_PATH = str(Path(__file__).resolve().parent / "_entry.py")
_CHILD_PYTHON = "3.13"
_ENVIRONMENT_HARBOR_EXTRAS: dict[EnvironmentType, str] = {
    EnvironmentType.DAYTONA: "daytona",
    EnvironmentType.MODAL: "modal",
    EnvironmentType.E2B: "e2b",
    EnvironmentType.RUNLOOP: "runloop",
    EnvironmentType.GKE: "gke",
    EnvironmentType.NOVITA: "novita",
    EnvironmentType.TENSORLAKE: "tensorlake",
    EnvironmentType.CWSANDBOX: "cwsandbox",
    EnvironmentType.WANDB: "wandb",
    EnvironmentType.ISLO: "islo",
}


class HarborOverrideImportError(Exception):
    """The override Harbor env could not start."""


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
    if "claude-code" in (
        agent or ""
    ).strip().lower() and _claude_code_forces_direct_api(is_probe):
        env.update({var: "" for var in BEDROCK_ENV_VARS})
    return env


def _build_payload(
    *,
    task_path: Path,
    jobs_dir: Path,
    outcome_path: Path,
    agent: str,
    model: str | None,
    environment: EnvironmentType,
    raw_harbor_config: dict[str, Any],
    is_probe: bool,
    extra_agent_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    daytona_kwargs: dict[str, Any] = {}
    if environment == EnvironmentType.DAYTONA:
        daytona_kwargs = {
            "auto_stop_interval_mins": settings.daytona_auto_stop_interval_mins,
            "auto_delete_interval_mins": settings.daytona_auto_delete_interval_mins,
            "ephemeral": settings.daytona_ephemeral,
        }
    return {
        "task_path": str(task_path),
        "jobs_dir": str(jobs_dir),
        "outcome_path": str(outcome_path),
        "agent": agent,
        "model": model,
        "environment": environment.value,
        "environment_config": raw_harbor_config.get("environment") or {},
        "daytona_kwargs": daytona_kwargs,
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
        "environment_build_timeout_multiplier": raw_harbor_config.get(
            "environment_build_timeout_multiplier"
        ),
        "retry": raw_harbor_config.get("retry"),
        "runtime_env": _runtime_env_overrides(
            agent=agent,
            model=model,
            raw_harbor_config=raw_harbor_config,
            is_probe=is_probe,
        ),
        "probe_task_dir": str(task_path) if is_probe else None,
        "probe_harness_dir": PROBE_HARNESS_DIR,
        "extra_agent_env": extra_agent_env or {},
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
                pass
        else:
            tail.append(line)
            del tail[:-50]


async def run_ephemeral_harbor_trial(
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
    """Run one trial in an override Harbor child process."""
    raw = harbor_config or {}
    hc = HarborConfig.model_validate(raw)
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

    is_probe = raw.get("mode") == "probe"
    if not is_probe:
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
    payload = _build_payload(
        task_path=effective_task_path,
        jobs_dir=unique_parent,
        outcome_path=outcome_path,
        agent=agent,
        model=model,
        environment=environment,
        raw_harbor_config=raw,
        is_probe=is_probe,
        extra_agent_env=extra_agent_env,
    )
    payload_path = unique_parent / "payload.json"
    payload_path.write_text(json.dumps(payload))

    start = time.time()
    tail: list[str] = []
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *_spawn_args(source, sha, environment=environment),
            str(payload_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            while True:
                chunk = await process.stderr.readline()
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
    finally:
        if task_tmpdir is not None:
            task_tmpdir.cleanup()


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
