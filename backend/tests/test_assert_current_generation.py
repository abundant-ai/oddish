"""Tests for .github/scripts/preview/assert_current_generation.py.

The fence compares the run's expected head SHA against the live branch tip:
staleness is neutral (exit 0, current=false) while an API outage fails closed
(exit 1) after exhausting retries.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/preview/assert_current_generation.py"
)
spec = importlib.util.spec_from_file_location("assert_current_generation", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

HEAD_SHA = "a" * 40
TIP_SHA = "b" * 40


@pytest.fixture
def env(monkeypatch, tmp_path):
    out = tmp_path / "out"
    out.write_text("")
    monkeypatch.setenv("OWNER_REPO", "abundant-ai/oddish")
    monkeypatch.setenv("HEAD_REF", "feature/x")
    monkeypatch.setenv("HEAD_SHA", HEAD_SHA)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(guard.time, "sleep", lambda _: None)
    return out


def test_current_generation(env, monkeypatch, capsys):
    monkeypatch.setattr(guard, "branch_tip_sha", lambda *_: HEAD_SHA)
    guard.main()
    assert capsys.readouterr().out.strip() == "current=true"
    assert "current=true" in env.read_text()


def test_stale_generation_is_neutral(env, monkeypatch, capsys):
    monkeypatch.setattr(guard, "branch_tip_sha", lambda *_: TIP_SHA)
    guard.main()  # must not raise SystemExit: supersession is not a failure
    captured = capsys.readouterr()
    assert captured.out.strip() == "current=false"
    assert "superseded" in captured.err
    assert "current=false" in env.read_text()


def test_api_failure_fails_closed_after_retries(env, monkeypatch, capsys):
    calls = []

    def _raise(*_):
        calls.append(1)
        raise subprocess.CalledProcessError(1, ["gh", "api"])

    monkeypatch.setattr(guard, "branch_tip_sha", _raise)
    with pytest.raises(SystemExit) as excinfo:
        guard.main()
    assert excinfo.value.code == 1
    assert len(calls) == guard.ATTEMPTS
    assert "could not verify preview generation" in capsys.readouterr().err


def test_transient_failure_recovers(env, monkeypatch):
    attempts = []

    def _flaky(*_):
        attempts.append(1)
        if len(attempts) < 2:
            raise subprocess.CalledProcessError(1, ["gh", "api"])
        return HEAD_SHA

    monkeypatch.setattr(guard, "branch_tip_sha", _flaky)
    guard.main()
    assert len(attempts) == 2
    assert "current=true" in env.read_text()


def test_branch_names_are_url_quoted(monkeypatch):
    seen = {}

    class Result:
        stdout = '{"commit": {"sha": "abc"}}'

    def _run(cmd, **kwargs):
        seen["cmd"] = cmd
        return Result()

    monkeypatch.setattr(guard.subprocess, "run", _run)
    assert guard.branch_tip_sha("o/r", "feat/a/b") == "abc"
    assert "feat%2Fa%2Fb" in seen["cmd"][-1]
