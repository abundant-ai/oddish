from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app
backfill_mod = sys.modules["oddish.cli.backfill_analysis"]

runner = CliRunner()


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _patch_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "k")
    monkeypatch.setenv("ODDISH_API_URL", "http://api.test")


def test_requires_exactly_one_scope(monkeypatch):
    _patch_env(monkeypatch)
    result = runner.invoke(app, ["backfill-analysis"])
    assert result.exit_code == 1
    assert "exactly one" in result.output.lower()


def test_task_scope_posts_once(monkeypatch):
    _patch_env(monkeypatch)
    posted = []

    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            posted.append((url, json))
            return _Resp(200, {"status": "queued", "task_id": "tsk", "trial_count": 3, "reset_count": 0})

    monkeypatch.setattr(backfill_mod.httpx, "Client", _Client)
    result = runner.invoke(app, ["backfill-analysis", "--task", "tsk"])
    assert result.exit_code == 0, result.output
    assert posted == [("http://api.test/tasks/tsk/qa/backfill",
                       {"force": False, "enable_analysis": False, "trial_ids": None})]


def test_trial_scope_resolves_task_and_sends_trial_ids(monkeypatch):
    _patch_env(monkeypatch)
    posted = []

    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            posted.append((url, json))
            return _Resp(200, {"status": "queued", "task_id": "tsk", "trial_count": 3, "reset_count": 1})

    monkeypatch.setattr(backfill_mod.httpx, "Client", _Client)
    result = runner.invoke(app, ["backfill-analysis", "--trial", "tsk-2", "--force"])
    assert result.exit_code == 0, result.output
    assert posted == [("http://api.test/tasks/tsk/qa/backfill",
                       {"force": True, "enable_analysis": False, "trial_ids": ["tsk-2"]})]


def test_experiment_scope_fans_out_over_tasks(monkeypatch):
    _patch_env(monkeypatch)
    posted = []

    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            assert params == {"experiment_id": "exp1"}
            return _Resp(200, [{"id": "t1"}, {"id": "t2"}])
        def post(self, url, json=None):
            posted.append(url)
            return _Resp(200, {"status": "queued", "task_id": url, "trial_count": 1, "reset_count": 0})

    monkeypatch.setattr(backfill_mod.httpx, "Client", _Client)
    result = runner.invoke(app, ["backfill-analysis", "--experiment", "exp1"])
    assert result.exit_code == 0, result.output
    assert posted == ["http://api.test/tasks/t1/qa/backfill",
                      "http://api.test/tasks/t2/qa/backfill"]
