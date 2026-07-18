"""Unit tests for bulk-retry trial selection (skipped exclusion)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.cli.api as api_mod  # noqa: E402
import oddish.cli.retry as retry_mod  # noqa: E402
from oddish.cli.retry import _failed_trial_ids  # noqa: E402


def _task():
    return {
        "trials": [
            {"id": "t-1", "status": "failed"},
            {"id": "t-2", "status": "skipped"},
            {"id": "t-3", "status": "success"},
            {"id": "t-4", "status": "skipped", "superseded_by_trial_id": "t-9"},
            {"id": "t-5", "status": "queued"},
        ]
    }


def test_bulk_retry_excludes_skipped_by_default():
    # Default bulk retry sweeps only FAILED; gate-skipped trials are left alone.
    assert _failed_trial_ids(_task()) == ["t-1"]


def test_bulk_retry_includes_skipped_when_opted_out():
    # --no-baseline-gate -> include_skipped: FAILED + live SKIPPED (not the
    # superseded one).
    assert _failed_trial_ids(_task(), include_skipped=True) == ["t-1", "t-2"]


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, captured, payload):
        self._captured = captured
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, _url, params=None):
        self._captured.append(params or {})
        return _Resp(self._payload)

    def post(self, _url, json=None):
        return _Resp({"ok": True})


@pytest.fixture
def captured_params(monkeypatch):
    captured: list[dict] = []
    payload = [{"id": "task-1", "trials": _task()["trials"]}]
    # api and retry share the same httpx module object, so one patch covers both.
    monkeypatch.setattr(api_mod, "get_auth_headers", lambda: {})
    monkeypatch.setattr(retry_mod, "get_auth_headers", lambda: {})
    monkeypatch.setattr(
        api_mod.httpx, "Client", lambda **_kw: _Client(captured, payload)
    )
    return captured


def test_get_experiment_tasks_omits_trials_by_default(captured_params):
    api_mod.get_experiment_tasks("http://api", "exp-1")
    assert "include_trials" not in captured_params[0]


def test_experiment_bulk_retry_requests_trials(captured_params):
    # Regression: the experiment sweep reads task["trials"], so it must ask the
    # API to embed them. Without include_trials every task comes back trial-less
    # and the sweep silently reports "No failed trials to retry."
    result = retry_mod._run_trial_retries(
        "http://api",
        "experiment",
        "exp-1",
        yes=True,
        json_output=True,
        registry_auth=None,
        gate_baselines=True,
    )
    assert captured_params[0].get("include_trials") == "true"
    assert result.get("note") != "No failed trials to retry."
