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
from harbor.trial.hooks import TrialEvent

from oddish.core.harbor_source import harbor_git_requirement
from oddish.workers.harbor import ephemeral as harbor_ephemeral
from oddish.workers.harbor._entry import _ProbeClaudeCode, _build_job_config
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


def test_build_payload_carries_extra_agent_env():
    payload = _build_payload(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="claude-sonnet-4-5",
        environment=EnvironmentType.DOCKER,
        raw_harbor_config={},
        is_probe=True,
        extra_agent_env={"ODDISH_API_KEY": "secret-mint"},
    )
    assert payload["extra_agent_env"] == {"ODDISH_API_KEY": "secret-mint"}


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


_SOURCE = "https://github.com/dot-agi/harbor"
_SHA = "a" * 40


def _payload(**over):
    base = dict(
        task_path=Path("/tmp/task"),
        jobs_dir=Path("/tmp/jobs"),
        outcome_path=Path("/tmp/jobs/outcome.json"),
        agent="claude-code",
        model="claude-sonnet-4-5",
        environment=EnvironmentType.DOCKER,
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
