"""Standalone, Harbor-only entrypoint for an ephemeral out-of-process trial.

Run by the parent worker as::

    uv run --no-project --with "harbor @ git+<source>@<sha>" --python 3.13 \
        <this file> <payload.json>

``--no-project`` gives it a fresh env containing ONLY the override Harbor and its
own transitive deps (importing oddish would re-pull the baked Harbor and defeat
the isolation). It therefore imports **only Harbor + stdlib**: it rebuilds a
``JobConfig`` from the JSON payload, runs ``Job.create``/``Job.run``, streams each
lifecycle event to stdout as one JSON line (NDJSON), and writes the final outcome
pointers to ``outcome.json``. The parent maps those back onto the trial.

``sys.path[0]`` (this file's own directory) is scrubbed at import so a stray
``import <sibling>`` can never reach into the oddish package and break the
Harbor-only isolation.
"""

from __future__ import annotations

import os
import sys

# When invoked BY PATH (``python .../_entry.py``), Python puts this file's
# own directory on ``sys.path[0]``, which would make sibling oddish modules
# top-level importable and could break the Harbor-only isolation. Drop it BEFORE
# importing anything else. (Guarded to the exact script dir so a normal
# ``import`` of this module -- where sys.path[0] is the cwd/'' -- is untouched.)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0] or "") == _THIS_DIR:
    sys.path.pop(0)

import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import shlex  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from harbor.agents.installed.claude_code import ClaudeCode  # noqa: E402

logger = logging.getLogger("oddish.harbor_entry")

# Event sentinel so the parent can distinguish our NDJSON event lines from any
# other stdout Harbor (or its deps) may emit.
EVENT_SENTINEL = "_oddish_harbor_event"


def _apply_sibling_harbor_patches() -> None:
    spec = importlib.util.spec_from_file_location(
        "_oddish_harbor_patches", Path(_THIS_DIR) / "patches.py"
    )
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.apply_harbor_patches()


def _emit_event_line(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _serialize_result(result: Any) -> dict[str, Any] | None:
    """Extract the JSON-able subset of a Harbor ``TrialResult`` the parent reads.

    Defensive/``getattr``-based so it tolerates Harbor-version field drift.
    """
    if result is None:
        return None
    out: dict[str, Any] = {}
    verifier_result = getattr(result, "verifier_result", None)
    rewards = getattr(verifier_result, "rewards", None) if verifier_result else None
    if rewards:
        try:
            out["verifier_result"] = {"rewards": dict(rewards)}
        except Exception:
            out["verifier_result"] = None
    exc = getattr(result, "exception_info", None)
    if exc is not None:
        out["exception_info"] = {
            "exception_type": getattr(exc, "exception_type", None),
            "exception_message": getattr(exc, "exception_message", None),
        }
    return out or None


def _make_hook(probe_task_dir: str | None, probe_harness_dir: str | None):
    async def _hook(event: Any) -> None:
        try:
            _emit_event_line(
                {
                    EVENT_SENTINEL: True,
                    "event": getattr(event.event, "value", str(event.event)),
                    "trial_id": getattr(event, "trial_id", None),
                    "environment_provider": getattr(
                        event, "environment_provider", None
                    ),
                    "environment_external_id": getattr(
                        event, "environment_external_id", None
                    ),
                    "result": _serialize_result(getattr(event, "result", None)),
                }
            )
        except Exception:
            # Events are advisory; never let a serialization slip kill the run.
            pass

        # The probe harness upload needs the LIVE environment handle, which only
        # exists here in the child. Best-effort, AGENT_START only.
        event_name = getattr(event.event, "value", str(event.event))
        environment = getattr(event, "environment", None)
        if (
            event_name == "AGENT_START"
            and probe_task_dir
            and probe_harness_dir
            and environment is not None
        ):
            try:
                await environment.upload_dir(
                    source_dir=Path(probe_task_dir), target_dir=probe_harness_dir
                )
            except Exception:
                pass

    return _hook


class _ProbeClaudeCode(ClaudeCode):
    """Claude Code that also installs the override Harbor into the sandbox.

    The in-process / blessed paths use ``OddishClaudeCode`` for this, but the
    ephemeral child runs ``uv run --no-project`` so oddish is absent. Instead we
    subclass Harbor's own ClaudeCode HERE (Harbor is the only dependency) and
    point ``AgentConfig.import_path`` at this class; Harbor resolves it via
    ``importlib`` against the running module, no oddish import needed. The parent
    supplies the exact git requirement (``harbor @ git+<source>@<sha>``) so the
    in-sandbox agent imports the SAME Harbor that scored the trial (S3).
    """

    def __init__(
        self, *args: Any, harbor_requirement: str | None = None, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        self._harbor_requirement = harbor_requirement

    async def install(self, environment: Any) -> None:
        await super().install(environment)
        if not self._harbor_requirement:
            return
        # Best-effort: a harbor-install failure must not fail the whole trial
        # (mirrors OddishClaudeCode / stage_harbor_source).
        command = f"pip install --user --quiet {shlex.quote(self._harbor_requirement)}"
        try:
            await self.exec_as_agent(environment, command=command)
        except Exception:
            logger.exception(
                "probe: failed to install %s into the ephemeral sandbox",
                self._harbor_requirement,
            )


def _build_job_config(payload: dict[str, Any]):
    from harbor import JobConfig
    from harbor.models.environment_type import EnvironmentType
    from harbor.models.job.config import RetryConfig
    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        VerifierConfig,
    )

    env_config = EnvironmentConfig.model_validate(
        payload.get("environment_config") or {}
    )
    env_config.type = EnvironmentType(payload["environment"])
    if env_config.type == EnvironmentType.DAYTONA and payload.get("daytona_kwargs"):
        env_config.kwargs = {**payload["daytona_kwargs"], **(env_config.kwargs or {})}

    agent_kwargs: dict[str, Any] = {}
    if payload.get("model"):
        agent_kwargs["model_name"] = payload["model"]
    if payload.get("extra_agent_env"):
        # Minted read-only oddish CLI creds for probe trials (parent-supplied).
        agent_kwargs["env"] = dict(payload["extra_agent_env"])

    agent_harbor_requirement = payload.get("agent_harbor_requirement")
    if agent_harbor_requirement:
        # Probe claude-code trial on an override ref: route the in-sandbox agent
        # through the installing subclass so it imports the override Harbor. The
        # import_path is resolved against THIS module (``__main__`` in the child),
        # so no oddish import is needed.
        agent_config = AgentConfig(
            name=None,
            import_path=f"{__name__}:_ProbeClaudeCode",
            kwargs={"harbor_requirement": agent_harbor_requirement},
            **agent_kwargs,
        )
    else:
        agent_config = AgentConfig(name=payload["agent"], **agent_kwargs)

    kwargs: dict[str, Any] = {
        "tasks": [TaskConfig(path=Path(payload["task_path"]))],
        "agents": [agent_config],
        "environment": env_config,
        "verifier": VerifierConfig.model_validate(payload.get("verifier") or {}),
        "artifacts": payload.get("artifacts") or [],
        "jobs_dir": Path(payload["jobs_dir"]),
    }
    for key in (
        "timeout_multiplier",
        "agent_timeout_multiplier",
        "verifier_timeout_multiplier",
        "agent_setup_timeout_multiplier",
        "environment_build_timeout_multiplier",
    ):
        if payload.get(key) is not None:
            kwargs[key] = payload[key]
    if payload.get("retry") is not None:
        kwargs["retry"] = RetryConfig.model_validate(payload["retry"])
    return JobConfig(**kwargs)


async def _run(payload: dict[str, Any]) -> dict[str, Any]:
    from harbor import Job

    _apply_sibling_harbor_patches()
    start = time.time()
    config = _build_job_config(payload)
    job = await Job.create(config)
    job_dir = Path(job.job_dir)

    hook = _make_hook(payload.get("probe_task_dir"), payload.get("probe_harness_dir"))
    for register in (
        "on_trial_started",
        "on_environment_started",
        "on_agent_started",
        "on_verification_started",
        "on_trial_ended",
        "on_trial_cancelled",
    ):
        getattr(job, register)(hook)

    await job.run()
    duration = time.time() - start
    job_result_path = job_dir / "result.json"
    return {
        "job_dir": str(job_dir),
        "job_result_path": str(job_result_path) if job_result_path.exists() else None,
        "duration_sec": duration,
        "error": None,
        "exception_type": None,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: _entry.py <payload.json>\n")
        return 2
    payload = json.loads(Path(argv[1]).read_text())

    # Apply runtime env overrides (provider routing/creds) computed by the parent.
    for key, value in (payload.get("runtime_env") or {}).items():
        os.environ[key] = value

    outcome_path = Path(payload["outcome_path"])
    start = time.time()
    try:
        outcome = asyncio.run(_run(payload))
    except BaseException as exc:  # noqa: BLE001 - report every failure to the parent
        outcome = {
            "job_dir": payload.get("jobs_dir"),
            "job_result_path": None,
            "duration_sec": time.time() - start,
            "error": f"{type(exc).__name__}: {exc}",
            "exception_type": type(exc).__name__,
            "traceback": traceback.format_exc()[-4000:],
        }
        outcome_path.write_text(json.dumps(outcome))
        # Non-zero exit signals the parent that the run did not complete cleanly.
        return 1
    outcome_path.write_text(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
