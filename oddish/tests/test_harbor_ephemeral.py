"""Parent-side bridge for the ephemeral out-of-process Harbor engine.

Uses a fake child script (no Harbor/Docker) to exercise the full subprocess
stream + outcome bridge fast and deterministically. The real older-Harbor child
is covered separately in ``test_harbor_ephemeral_real.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time

import pytest

from harbor.trial.hooks import TrialEvent

from oddish.workers import harbor_ephemeral
from oddish.workers._harbor_entry import _build_job_config
from oddish.workers.harbor_ephemeral import (
    HarborOverrideImportError,
    _bridge_event,
    _build_payload,
    _read_outcome,
    run_ephemeral_harbor_trial,
)
from oddish.workers.harbor_outcome import HarborOutcome

_EPHEMERAL_HC = {
    "variant_id": "ephemeral",
    "source": "https://github.com/dot-agi/harbor",
    "resolved_sha": "a" * 40,
}


# --------------------------------------------------------------------------- #
# _bridge_event
# --------------------------------------------------------------------------- #


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
    assert ev.environment is None  # live handle never crosses the boundary
    assert ev.environment_external_id == "sb-9"
    assert ev.result.verifier_result.rewards["reward"] == 1.0
    assert ev.result.exception_info.exception_type == "AgentTimeoutError"


def test_bridge_event_non_end_has_no_result():
    ev = _bridge_event({"event": "agent_start", "trial_id": "t-1"}, trial_id="t-1")
    assert ev.event == TrialEvent.AGENT_START
    assert ev.result is None
    assert ev.trial_id == "t-1"


# --------------------------------------------------------------------------- #
# _read_outcome
# --------------------------------------------------------------------------- #


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
        json.dumps(
            {"job_dir": str(tmp_path), "job_result_path": None, "error": "boom"}
        )
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


# --------------------------------------------------------------------------- #
# extra_agent_env (minted probe creds) threads to the child's agent
# --------------------------------------------------------------------------- #


def test_build_payload_carries_extra_agent_env():
    from pathlib import Path

    from harbor.models.environment_type import EnvironmentType

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


def test_read_outcome_with_result_json_uses_extractor(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    result_path.write_text("{}")
    (tmp_path / "outcome.json").write_text(
        json.dumps(
            {"job_dir": str(tmp_path), "job_result_path": str(result_path)}
        )
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


# --------------------------------------------------------------------------- #
# run_ephemeral_harbor_trial with a fake child (full subprocess flow)
# --------------------------------------------------------------------------- #

_FAKE_CHILD = textwrap.dedent(
    """
    import json, sys
    payload = json.loads(open(sys.argv[1]).read())
    sentinel = "_oddish_harbor_event"
    for ev in ("start", "agent_start"):
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
    monkeypatch.setattr(harbor_ephemeral, "validate_task_timeout_config", lambda p: None)
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *a, **k: None
    )
    child = tmp_path / "fake_child.py"
    child.write_text(_FAKE_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral, "_spawn_args", lambda s, sha: [sys.executable, str(child)]
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

    # Events bridged in order; the noisy non-event log line was ignored.
    assert seen == ["start", "agent_start", "end"]
    # No result.json from the fake child -> terminal override error.
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
    monkeypatch.setattr(harbor_ephemeral, "validate_task_timeout_config", lambda p: None)
    monkeypatch.setattr(
        harbor_ephemeral, "_check_local_storage_preflight", lambda *a, **k: None
    )
    child = tmp_path / "sleep_child.py"
    child.write_text(_SLEEP_CHILD)
    monkeypatch.setattr(
        harbor_ephemeral, "_spawn_args", lambda s, sha: [sys.executable, str(child)]
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
    # Wait for the child to come up and record its pid.
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

    # The child (and its group) must be dead, not orphaned.
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
