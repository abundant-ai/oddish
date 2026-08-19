"""Test the ephemeral Harbor bridge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import EnvironmentConfig
from harbor.trial.hooks import TrialEvent

from oddish.core.harbor_source import harbor_git_requirement
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.workers.harbor import ephemeral as harbor_ephemeral
from oddish.workers.harbor._entry import (
    _ProbeClaudeCode,
    _build_job_config,
    _read_payload_and_unlink,
)
from oddish.workers.harbor.ephemeral import (
    HarborOverrideImportError,
    _bridge_event,
    _build_payload,
    _read_outcome,
    run_ephemeral_harbor_trial,
)
from oddish.workers.harbor.outcome import HarborOutcome

_EPHEMERAL_HC = {
    "variant_id": "ephemeral",
    "source": "https://github.com/dot-agi/harbor",
    "resolved_sha": "a" * 40,
}


def test_bridge_event_end_with_reward_and_exception():
    ev = _bridge_event(
        {
            "event": "end",
            "trial_id": "t-1",
            "environment_provider": "modal",
            "environment_external_id": "sb-9",
            "result": {
                "verifier_result": {"rewards": {"reward": 1.0}},
                "exception_info": {
                    "exception_type": "AgentTimeoutError",
                    "exception_message": "slow",
                },
            },
        },
        trial_id="t-1",
    )
    assert ev.event == TrialEvent.END
    assert ev.environment is None
    assert ev.environment_external_id == "sb-9"
    assert ev.result.verifier_result.rewards["reward"] == 1.0
    assert ev.result.exception_info.exception_type == "AgentTimeoutError"


@pytest.mark.parametrize(
    "event_name, expected",
    [
        ("START", TrialEvent.START),
        (TrialEvent.AGENT_START.value, TrialEvent.AGENT_START),
        ("AGENT_START", TrialEvent.AGENT_START),
        ("agent_start", TrialEvent.AGENT_START),
        ("ENVIRONMENT_START", TrialEvent.ENVIRONMENT_START),
        ("environment_start", TrialEvent.ENVIRONMENT_START),
        ("VERIFICATION_START", TrialEvent.VERIFICATION_START),
        ("verification_start", TrialEvent.VERIFICATION_START),
        ("AGENT_END", TrialEvent.AGENT_END),
        ("agent_end", TrialEvent.AGENT_END),
        ("END", TrialEvent.END),
        ("CANCEL", TrialEvent.CANCEL),
    ],
)
def test_bridge_event_non_end_has_no_result(event_name, expected):
    ev = _bridge_event({"event": event_name, "trial_id": "t-1"}, trial_id="t-1")
    assert ev.event == expected
    assert ev.result is None
    assert ev.trial_id == "t-1"


def test_read_outcome_missing_outcome_json_is_non_retryable(tmp_path):
    outcome = _read_outcome(
        outcome_path=tmp_path / "outcome.json",
        unique_parent=tmp_path,
        returncode=1,
        duration=2.0,
        stderr="No solution found: harbor requires-python",
        stdout_tail="",
    )
    assert outcome.exception_type == "HarborOverrideImportError"
    assert "No solution found" in outcome.error


def test_read_outcome_without_result_json_is_non_retryable(tmp_path):
    (tmp_path / "outcome.json").write_text(
        json.dumps({"job_dir": str(tmp_path), "job_result_path": None, "error": "boom"})
    )
    outcome = _read_outcome(
        outcome_path=tmp_path / "outcome.json",
        unique_parent=tmp_path,
        returncode=1,
        duration=1.0,
        stderr="",
        stdout_tail="",
    )
    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.error == "boom"


def test_build_payload_prefers_passed_env_build_multiplier():
    # The runner computes the GKE-sized env-build multiplier and passes it in; it
    # must win over whatever rode in the raw config, so the child JobConfig gets
    # the pod-ready-covering value.
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="nop",
        model=None,
        environment_config=EnvironmentConfig(type=EnvironmentType.GKE),
        raw_harbor_config={"environment_build_timeout_multiplier": 2.0},
        is_probe=False,
        environment_build_timeout_multiplier=99.0,
    )
    assert payload["environment_build_timeout_multiplier"] == 99.0


def test_build_payload_falls_back_to_raw_env_build_multiplier():
    # With nothing passed (the off-GKE case), the child keeps the caller's raw
    # multiplier -- behavior-preserving for non-GKE ephemeral trials.
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="nop",
        model=None,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={"environment_build_timeout_multiplier": 2.0},
        is_probe=False,
    )
    assert payload["environment_build_timeout_multiplier"] == 2.0


def test_build_payload_carries_extra_agent_env():
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="claude-sonnet-4-5",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={},
        is_probe=True,
        extra_agent_env={"ODDISH_API_KEY": "secret-mint"},
    )
    assert payload["extra_agent_env"] == {"ODDISH_API_KEY": "secret-mint"}


def test_build_payload_carries_agent_config():
    agent_config = {
        "env": {"UV_HTTP_RETRIES": "8"},
        "extra_allowed_hosts": ["astral.sh", "pypi.org"],
        "override_setup_timeout_sec": 1800,
    }
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="mini-swe-agent",
        model="openrouter/tencent/hy3",
        environment=EnvironmentType.MODAL,
        raw_harbor_config={"agent_config": agent_config},
        is_probe=False,
    )
    assert payload["agent_config"] == agent_config


def test_child_applies_submitted_agent_config(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "mini-swe-agent",
            "model": "openrouter/tencent/hy3",
            "environment": "modal",
            "environment_config": {},
            "agent_config": {
                "env": {"UV_HTTP_RETRIES": "8"},
                "extra_allowed_hosts": ["astral.sh", "pypi.org"],
                "override_setup_timeout_sec": 1800,
            },
            "verifier": {},
            "artifacts": [],
        }
    )
    agent_config = config.agents[0]
    assert agent_config.name == "mini-swe-agent"
    assert agent_config.model_name == "openrouter/tencent/hy3"
    assert agent_config.env == {"UV_HTTP_RETRIES": "8"}
    assert agent_config.extra_allowed_hosts == ["astral.sh", "pypi.org"]
    assert agent_config.override_setup_timeout_sec == 1800


def test_child_preserves_submitted_agent_import_path(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "ignored-built-in-name",
            "model": "custom/model",
            "environment": "docker",
            "environment_config": {},
            "agent_config": {"import_path": "custom.module:CustomAgent"},
            "verifier": {},
            "artifacts": [],
        }
    )
    assert config.agents[0].name is None
    assert config.agents[0].import_path == "custom.module:CustomAgent"
    assert config.agents[0].model_name == "custom/model"


def test_child_applies_extra_agent_env_to_agent_config(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "claude-code",
            "model": "claude-sonnet-4-5",
            "environment": "docker",
            "environment_config": {},
            "verifier": {},
            "artifacts": [],
            "extra_agent_env": {"ODDISH_API_KEY": "secret-mint"},
        }
    )
    assert config.agents[0].env.get("ODDISH_API_KEY") == "secret-mint"


def test_child_merges_worker_env_over_submitted_env(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "mini-swe-agent",
            "model": "openrouter/tencent/hy3",
            "environment": "modal",
            "environment_config": {},
            "agent_config": {
                "env": {
                    "UV_HTTP_RETRIES": "8",
                    "OPENAI_BASE_URL": "https://stale.invalid",
                    "ODDISH_API_KEY": "stale",
                }
            },
            "verifier": {},
            "artifacts": [],
            "runtime_env": {"OPENAI_BASE_URL": "https://worker.example/v1"},
            "extra_agent_env": {"ODDISH_API_KEY": "secret-mint"},
        }
    )
    assert config.agents[0].env == {
        "UV_HTTP_RETRIES": "8",
        "OPENAI_BASE_URL": "https://worker.example/v1",
        "ODDISH_API_KEY": "secret-mint",
    }


_SOURCE = "https://github.com/dot-agi/harbor"
_SHA = "a" * 40


def _payload(**over):
    base = dict(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="claude-sonnet-4-5",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        raw_harbor_config={"source": _SOURCE, "resolved_sha": _SHA},
        is_probe=True,
    )
    base.update(over)
    return _build_payload(**base)


def test_payload_agent_harbor_requirement_is_override_git_req_for_probe_claude_code():
    req = _payload()["agent_harbor_requirement"]
    assert req == harbor_git_requirement(_SOURCE, _SHA)
    assert req == f"harbor @ git+{_SOURCE}@{_SHA}"
    assert _SHA in req


def test_payload_no_agent_harbor_requirement_for_non_probe():
    assert _payload(is_probe=False)["agent_harbor_requirement"] is None


def test_payload_no_agent_harbor_requirement_for_non_claude_code_agent():
    assert _payload(agent="codex")["agent_harbor_requirement"] is None


def test_payload_agent_harbor_requirement_is_exact_match_not_substring():
    assert _payload(agent="claude-code-custom")["agent_harbor_requirement"] is None
    assert _payload(agent="my-claude-code")["agent_harbor_requirement"] is None
    assert _payload(agent="Claude-Code")["agent_harbor_requirement"] == (
        harbor_git_requirement(_SOURCE, _SHA)
    )


def test_child_routes_probe_agent_through_installing_subclass(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    req = harbor_git_requirement(_SOURCE, _SHA)
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "claude-code",
            "model": "claude-sonnet-4-5",
            "environment": "docker",
            "environment_config": {},
            "verifier": {},
            "artifacts": [],
            "agent_harbor_requirement": req,
        }
    )
    ac = config.agents[0]
    assert ac.name is None
    assert ac.import_path.endswith(":_ProbeClaudeCode")
    assert ac.kwargs["harbor_requirement"] == req
    from harbor.utils.import_path import import_class

    assert import_class(ac.import_path) is _ProbeClaudeCode


def test_child_default_agent_not_routed_through_subclass(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    config = _build_job_config(
        {
            "task_path": str(task_dir),
            "jobs_dir": str(tmp_path / "jobs"),
            "agent": "claude-code",
            "model": "claude-sonnet-4-5",
            "environment": "docker",
            "environment_config": {},
            "verifier": {},
            "artifacts": [],
        }
    )
    ac = config.agents[0]
    assert ac.name == "claude-code"
    assert ac.import_path is None


@pytest.mark.asyncio
async def test_probe_claude_code_installs_override_harbor(tmp_path, monkeypatch):
    req = harbor_git_requirement(_SOURCE, _SHA)
    agent = _ProbeClaudeCode(
        logs_dir=tmp_path, model_name="claude-sonnet-4-5", harbor_requirement=req
    )

    calls: list[str] = []

    async def _fake_super_install(self, environment):
        calls.append("super")

    async def _fake_exec(self, environment, *, command):
        calls.append(command)

    monkeypatch.setattr(
        "harbor.agents.installed.claude_code.ClaudeCode.install", _fake_super_install
    )
    monkeypatch.setattr(_ProbeClaudeCode, "exec_as_agent", _fake_exec)

    await agent.install(environment=object())

    assert calls[0] == "super"
    assert "pip install --user --quiet" in calls[1]
    assert req in calls[1]
    assert _SHA in calls[1]


@pytest.mark.asyncio
async def test_probe_claude_code_install_is_best_effort_on_failure(
    tmp_path, monkeypatch
):
    agent = _ProbeClaudeCode(
        logs_dir=tmp_path,
        model_name="claude-sonnet-4-5",
        harbor_requirement=harbor_git_requirement(_SOURCE, _SHA),
    )

    async def _fake_super_install(self, environment):
        pass

    async def _boom_exec(self, environment, *, command):
        raise RuntimeError("sandbox pip is down")

    monkeypatch.setattr(
        "harbor.agents.installed.claude_code.ClaudeCode.install", _fake_super_install
    )
    monkeypatch.setattr(_ProbeClaudeCode, "exec_as_agent", _boom_exec)

    await agent.install(environment=object())


def test_read_outcome_with_result_json_uses_extractor(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    result_path.write_text("{}")
    (tmp_path / "outcome.json").write_text(
        json.dumps({"job_dir": str(tmp_path), "job_result_path": str(result_path)})
    )
    sentinel = HarborOutcome(
        reward=1.0,
        error=None,
        exit_code=0,
        duration_sec=3.0,
        job_result_path=result_path,
        job_dir=tmp_path,
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_extract_outcome_from_job_result", lambda **k: sentinel
    )

    class _FakeJobResult:
        @staticmethod
        def model_validate_json(_text):
            return object()

    import harbor.models.job.result as result_mod

    monkeypatch.setattr(result_mod, "JobResult", _FakeJobResult)
    outcome = _read_outcome(
        outcome_path=tmp_path / "outcome.json",
        unique_parent=tmp_path,
        returncode=0,
        duration=3.0,
        stderr="",
        stdout_tail="",
    )
    assert outcome is sentinel


def test_harbor_override_import_error_is_non_retryable():
    from oddish.workers.queue.trial_handler import _NON_RETRYABLE_EXCEPTION_TYPES

    assert HarborOverrideImportError.__name__ in _NON_RETRYABLE_EXCEPTION_TYPES


def test_spawn_args_requests_daytona_extra_for_daytona_env():
    args = harbor_ephemeral._spawn_args(
        _SOURCE, _SHA, environment=EnvironmentType.DAYTONA
    )
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA, extras=["daytona"])
    assert req.startswith("harbor[daytona] @ git+")


def test_ephemeral_daytona_forces_ownership_labels():
    raw_kwargs = {
        "auto_labels": False,
        "labels": {
            "task": "x",
            "oddish.managed": "false",
            "oddish.expires_at": "0",
        },
    }
    resolved = EnvironmentConfig.model_validate(
        {
            "type": "daytona",
            "kwargs": DaytonaBackend().harbor_env_kwargs(raw_kwargs),
        }
    )
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="nop",
        model=None,
        environment_config=resolved,
        raw_harbor_config={},
        is_probe=False,
    )
    config = _build_job_config(payload)
    assert config.environment.kwargs["auto_labels"] is True
    labels = config.environment.kwargs["labels"]
    assert labels["task"] == "x"
    assert labels["oddish.managed"] == "true"
    assert int(labels["oddish.expires_at"]) > 0


def test_spawn_args_requests_ec2_extra_for_ec2_env():
    args = harbor_ephemeral._spawn_args(
        _SOURCE, _SHA, environment=EnvironmentType.EC2
    )
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA, extras=["ec2"])


def test_payload_serializes_resolved_environment_config_without_child_merges():
    resolved = EnvironmentConfig.model_validate(
        {
            "type": "daytona",
            "kwargs": {
                "keep": "value",
                "auto_stop_interval_mins": 17,
                "auto_delete_interval_mins": 23,
                "ephemeral": False,
            },
        }
    )
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="nop",
        model=None,
        environment_config=resolved,
        raw_harbor_config={},
        is_probe=False,
    )

    assert payload["environment_config"] == resolved.model_dump(mode="json")
    assert "daytona_kwargs" not in payload
    child_config = _build_job_config(
        {
            **payload,
            "daytona_kwargs": {"auto_stop_interval_mins": 999},
        }
    )
    assert child_config.environment.model_dump(mode="json") == (
        resolved.model_dump(mode="json")
    )


def test_spawn_args_no_extra_for_docker_env():
    args = harbor_ephemeral._spawn_args(
        _SOURCE, _SHA, environment=EnvironmentType.DOCKER
    )
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA)
    assert "[" not in req.split("@", 1)[0]


def test_spawn_args_defaults_to_docker_no_extra():
    args = harbor_ephemeral._spawn_args(_SOURCE, _SHA)
    req = args[args.index("--with") + 1]
    assert req == harbor_git_requirement(_SOURCE, _SHA)


def test_child_process_env_exposes_existing_parent_site_packages(monkeypatch, tmp_path):
    first = tmp_path / "site-a"
    second = tmp_path / "site-b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        harbor_ephemeral.site,
        "getsitepackages",
        lambda: [str(first), str(tmp_path / "missing"), str(second)],
    )

    env = harbor_ephemeral._child_process_env()

    assert env["ODDISH_PARENT_SITE_PACKAGES"].split(os.pathsep) == [
        str(first.resolve()),
        str(second.resolve()),
    ]


def test_child_process_env_fails_loudly_without_parent_site_packages(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        harbor_ephemeral.site,
        "getsitepackages",
        lambda: [str(tmp_path / "missing")],
    )

    with pytest.raises(
        HarborOverrideImportError,
        match="cannot locate parent site-packages",
    ):
        harbor_ephemeral._child_process_env()


_FAKE_CHILD = textwrap.dedent(
    """
    import json, sys
    payload = json.loads(open(sys.argv[1]).read())
    sentinel = "_oddish_harbor_event"
    for ev in ("start", "agent-start"):
        print(json.dumps({sentinel: True, "event": ev, "trial_id": payload.get("trial_id")}), flush=True)
    print("harbor: some noisy log line that is not an event", flush=True)
    print(json.dumps({sentinel: True, "event": "end", "trial_id": payload.get("trial_id"),
                      "result": {"verifier_result": {"rewards": {"reward": 1.0}}}}), flush=True)
    open(payload["outcome_path"], "w").write(json.dumps(
        {"job_dir": payload["jobs_dir"], "job_result_path": None,
        "duration_sec": 0.1, "error": "fake child: no result", "exception_type": None}))
    """
)


@pytest.mark.asyncio
async def test_run_ephemeral_streams_events_and_reads_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda p: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *a, **k: None
    )
    child = tmp_path / "fake_child.py"
    child.write_text(_FAKE_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda s, sha, **kw: [sys.executable, str(child)],
    )

    seen: list[str] = []

    async def hook(event):
        seen.append(event.event.value)

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    outcome = await run_ephemeral_harbor_trial(
        task_path=task_dir,
        agent="claude-code",
        jobs_dir=tmp_path / "jobs",
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        model="claude-sonnet-4-5",
        hook_callback=hook,
        trial_id="t-eph",
        harbor_config=_EPHEMERAL_HC,
    )

    assert seen == ["start", "agent-start", "end"]
    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.job_dir is not None


_SLEEP_CHILD = textwrap.dedent(
    """
    import json, os, sys, time
    payload = json.loads(open(sys.argv[1]).read())
    open(payload["jobs_dir"] + "/pid", "w").write(str(os.getpid()))
    time.sleep(120)
    """
)


_ENV_CHILD = textwrap.dedent(
    """
    import json, os, stat, sys
    payload = json.loads(open(sys.argv[1]).read())
    key_path = payload["environment_config"]["kwargs"]["ssh_key_path"]
    profile_path = os.environ["AWS_SHARED_CREDENTIALS_FILE"]
    open(payload["jobs_dir"] + "/child-env.json", "w").write(json.dumps({
        "secret": os.environ.get("ODDISH_EC2_SSH_PRIVATE_KEY"),
        "access": os.environ.get("ODDISH_EC2_AWS_ACCESS_KEY_ID"),
        "aws_secret": os.environ.get("ODDISH_EC2_AWS_SECRET_ACCESS_KEY"),
        "token": os.environ.get("ODDISH_EC2_AWS_SESSION_TOKEN"),
        "key_path": key_path,
        "key_mode": stat.S_IMODE(os.stat(key_path).st_mode),
        "profile": payload["environment_config"]["kwargs"]["aws_profile"],
        "profile_mode": stat.S_IMODE(os.stat(profile_path).st_mode),
    }))
    open(payload["outcome_path"], "w").write(json.dumps({
        "job_dir": payload["jobs_dir"],
        "job_result_path": None,
        "duration_sec": 0.1,
        "error": "fake child: no result",
        "exception_type": None,
    }))
    """
)


_FAILED_CHILD = textwrap.dedent(
    """
    import json, stat, sys
    from pathlib import Path

    payload_path = Path(sys.argv[1])
    payload = json.loads(payload_path.read_text())
    Path(payload["jobs_dir"], "payload-observation.json").write_text(json.dumps({
        "payload_path": str(payload_path),
        "payload_mode": stat.S_IMODE(payload_path.stat().st_mode),
    }))
    raise SystemExit(19)
    """
)


def test_child_unlinks_payload_even_when_json_is_invalid(tmp_path):
    payload_path = tmp_path / "invalid-payload.json"
    payload_path.write_text("not json")

    with pytest.raises(json.JSONDecodeError):
        _read_payload_and_unlink(payload_path)

    assert not payload_path.exists()


@pytest.mark.asyncio
async def test_failed_ephemeral_child_cannot_expose_payload_in_job_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda _path: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *_a, **_k: None
    )
    child = tmp_path / "failed_child.py"
    child.write_text(_FAILED_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda _source, _sha, **_kwargs: [sys.executable, str(child)],
    )
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    jobs_dir = tmp_path / "jobs"
    secret = "must-never-become-an-artifact"

    outcome = await run_ephemeral_harbor_trial(
        task_path=task_dir,
        agent="nop",
        jobs_dir=jobs_dir,
        environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
        harbor_config=_EPHEMERAL_HC,
        extra_agent_env={"PROVIDER_SECRET": secret},
    )

    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.job_dir is not None
    observation = json.loads(
        (outcome.job_dir / "payload-observation.json").read_text()
    )
    payload_path = Path(observation["payload_path"])
    assert payload_path.parent != outcome.job_dir
    assert observation["payload_mode"] == 0o600
    assert not payload_path.exists()
    artifact_contents = "\n".join(
        path.read_text(errors="replace")
        for path in outcome.job_dir.rglob("*")
        if path.is_file()
    )
    assert secret not in artifact_contents


@pytest.mark.asyncio
async def test_ephemeral_child_uses_key_path_without_inheriting_private_key_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda _path: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *_a, **_k: None
    )
    child = tmp_path / "env_child.py"
    child.write_text(_ENV_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda _source, _sha, **_kwargs: [sys.executable, str(child)],
    )
    monkeypatch.setenv("ODDISH_EC2_SSH_PRIVATE_KEY", "must-not-reach-child")
    monkeypatch.setenv("ODDISH_EC2_AWS_ACCESS_KEY_ID", "must-not-reach-child")
    monkeypatch.setenv("ODDISH_EC2_AWS_SECRET_ACCESS_KEY", "must-not-reach-child")
    monkeypatch.setenv("ODDISH_EC2_AWS_SESSION_TOKEN", "must-not-reach-child")
    key_path = tmp_path / "ec2-key"
    key_path.write_text("private key\n")
    key_path.chmod(0o600)
    profile_path = tmp_path / "aws-credentials"
    profile_path.write_text("[oddish-ec2]\n")
    profile_path.chmod(0o600)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(profile_path))
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    jobs_dir = tmp_path / "jobs"
    resolved = EnvironmentConfig.model_validate(
        {
            "type": "ec2",
            "kwargs": {
                "ssh_key_path": str(key_path),
                "aws_profile": "oddish-ec2",
            },
        }
    )

    await run_ephemeral_harbor_trial(
        task_path=task_dir,
        agent="nop",
        jobs_dir=jobs_dir,
        environment_config=resolved,
        harbor_config=_EPHEMERAL_HC,
    )

    child_state = json.loads(
        (next(jobs_dir.iterdir()) / "child-env.json").read_text()
    )
    assert child_state == {
        "secret": None,
        "access": None,
        "aws_secret": None,
        "token": None,
        "key_path": str(key_path),
        "key_mode": 0o600,
        "profile": "oddish-ec2",
        "profile_mode": 0o600,
    }


@pytest.mark.asyncio
async def test_run_ephemeral_cancel_kills_child(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harbor_ephemeral, "validate_task_timeout_config", lambda p: None
    )
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *a, **k: None
    )
    child = tmp_path / "sleep_child.py"
    child.write_text(_SLEEP_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral,
        "_spawn_args",
        lambda s, sha, **kw: [sys.executable, str(child)],
    )
    jobs_dir = tmp_path / "jobs"
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    task = asyncio.create_task(
        run_ephemeral_harbor_trial(
            task_path=task_dir,
            agent="nop",
            jobs_dir=jobs_dir,
            environment_config=EnvironmentConfig(type=EnvironmentType.DOCKER),
            trial_id="t-cancel",
            harbor_config=_EPHEMERAL_HC,
        )
    )
    pid_file = jobs_dir / "task.nop.t-cancel" / "pid"
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)
    assert pid_file.exists(), "child never started"
    pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_upload_probe_assets_splits_targets(tmp_path):
    from oddish.workers.queue.trial_handler import _upload_probe_assets
    from oddish.worker.probe_overlay import PROBE_HARNESS_DIR

    assets = tmp_path / "task"
    (assets / "solution").mkdir(parents=True)
    (assets / "solution" / "f.txt").write_text("x")

    uploads = []

    class FakeEnv:
        async def upload_dir(self, *, source_dir, target_dir):
            names = sorted(p.name for p in Path(source_dir).iterdir())
            uploads.append((target_dir, names))

    await _upload_probe_assets(FakeEnv(), assets, "t1")

    targets = {t for t, _ in uploads}
    # Only the CLI is uploaded; the STAGE_DIR asset push was dropped (Task 8).
    assert PROBE_HARNESS_DIR in targets
    # CLI mount carries ONLY the CLI.
    harness = next(names for t, names in uploads if t == PROBE_HARNESS_DIR)
    assert harness == ["oddish-query"]
