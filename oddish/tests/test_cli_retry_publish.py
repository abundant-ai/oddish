from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import httpx
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app

# ``oddish.cli`` re-exports the ``publish`` / ``delete`` command functions, which
# shadow the submodule attributes; import the actual modules via importlib.
publish_mod = importlib.import_module("oddish.cli.publish")
retry_mod = importlib.import_module("oddish.cli.retry")
delete_mod = importlib.import_module("oddish.cli.delete")
cancel_mod = importlib.import_module("oddish.cli.cancel")

runner = CliRunner()


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class _Client:
    """Minimal httpx.Client stand-in recording posted URLs."""

    def __init__(self, response: _Resp, calls: list[str]):
        self._response = response
        self._calls = calls

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, **_kwargs: object) -> _Resp:
        self._calls.append(url)
        return self._response


def _patch_key(monkeypatch) -> None:
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")


def test_publish_emits_share_url_json(monkeypatch) -> None:
    _patch_key(monkeypatch)
    calls: list[str] = []
    resp = _Resp(200, {"name": "exp1", "is_public": True, "public_token": "tok123"})
    monkeypatch.setattr(publish_mod.httpx, "Client", lambda **kw: _Client(resp, calls))

    result = runner.invoke(
        app,
        ["publish", "exp1", "--api", "http://api.test", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["http://api.test/experiments/exp1/publish"]
    data = json.loads(result.stdout)
    assert data["public_token"] == "tok123"
    assert data["public_url"].endswith("/share/tok123")
    assert data["is_public"] is True


def test_unpublish_posts_unpublish_endpoint(monkeypatch) -> None:
    _patch_key(monkeypatch)
    calls: list[str] = []
    resp = _Resp(200, {"name": "exp1", "is_public": False, "public_token": "tok123"})
    monkeypatch.setattr(publish_mod.httpx, "Client", lambda **kw: _Client(resp, calls))

    result = runner.invoke(
        app,
        ["unpublish", "exp1", "--api", "http://api.test", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["http://api.test/experiments/exp1/unpublish"]
    assert json.loads(result.stdout)["is_public"] is False


def test_publish_surfaces_error(monkeypatch) -> None:
    _patch_key(monkeypatch)
    resp = _Resp(404, {"detail": "Experiment not found"})
    monkeypatch.setattr(publish_mod.httpx, "Client", lambda **kw: _Client(resp, []))

    result = runner.invoke(
        app, ["publish", "missing", "--api", "http://api.test", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == 404


def test_retry_resolves_trial_and_posts_retry(monkeypatch) -> None:
    """A trial-shaped id that exists in its parent task hits /trials/{id}/retry."""
    _patch_key(monkeypatch)
    posted: list[str] = []

    def fake_get_task_summary(api_url: str, task_id: str):
        if task_id == "abc":
            return {"id": "abc", "trials": [{"id": "abc-0", "status": "failed"}]}
        return None

    monkeypatch.setattr(retry_mod, "get_task_summary", fake_get_task_summary)
    monkeypatch.setattr(retry_mod, "get_experiment_tasks", lambda *a, **k: None)

    def fake_post(api_url: str, path: str) -> _Resp:
        posted.append(path)
        return _Resp(200, {"status": "queued", "trial_id": "abc-1"})

    monkeypatch.setattr(retry_mod, "_post", fake_post)

    result = runner.invoke(
        app,
        ["run", "abc-0", "--retry", "-y", "--api", "http://api.test", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert posted == ["/trials/abc-0/retry"]
    data = json.loads(result.stdout)
    assert data["kind"] == "trials"
    assert data["queued"] == 1


def test_retry_task_only_retries_failed_trials(monkeypatch) -> None:
    _patch_key(monkeypatch)
    posted: list[str] = []

    def fake_get_task_summary(api_url: str, task_id: str):
        if task_id == "tsk":
            return {
                "id": "tsk",
                "trials": [
                    {"id": "tsk-0", "status": "failed"},
                    {"id": "tsk-1", "status": "success"},
                    {"id": "tsk-2", "status": "failed"},
                ],
            }
        return None

    monkeypatch.setattr(retry_mod, "get_task_summary", fake_get_task_summary)
    monkeypatch.setattr(retry_mod, "get_experiment_tasks", lambda *a, **k: None)

    def fake_post(api_url: str, path: str) -> _Resp:
        posted.append(path)
        return _Resp(200, {"status": "queued"})

    monkeypatch.setattr(retry_mod, "_post", fake_post)

    result = runner.invoke(
        app,
        ["run", "--task", "tsk", "--retry", "-y", "--api", "http://api.test", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert posted == ["/trials/tsk-0/retry", "/trials/tsk-2/retry"]


def test_retry_verdict_dispatches_task_endpoint(monkeypatch) -> None:
    _patch_key(monkeypatch)
    posted: list[str] = []
    monkeypatch.setattr(
        retry_mod, "get_task_summary", lambda a, t: {"id": t, "trials": []}
    )

    def fake_post(api_url: str, path: str) -> _Resp:
        posted.append(path)
        return _Resp(200, {"status": "queued", "task_id": "tsk"})

    monkeypatch.setattr(retry_mod, "_post", fake_post)

    result = runner.invoke(
        app,
        [
            "run",
            "--task",
            "tsk",
            "--retry",
            "--verdict",
            "-y",
            "--api",
            "http://api.test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert posted == ["/tasks/tsk/verdict/retry"]


def test_cancel_analysis_dispatches_task_endpoint(monkeypatch) -> None:
    _patch_key(monkeypatch)
    calls: list[str] = []
    resp = _Resp(200, {"status": "cancelled", "analysis_jobs_cancelled": 1})
    monkeypatch.setattr(cancel_mod.httpx, "Client", lambda **kw: _Client(resp, calls))
    monkeypatch.setattr(cancel_mod, "get_task_summary", lambda *a, **k: None)

    result = runner.invoke(
        app,
        ["cancel", "tsk", "--analysis", "--force", "--api", "http://api.test", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["http://api.test/tasks/tsk/analysis/cancel"]
    assert json.loads(result.stdout)["analysis_jobs_cancelled"] == 1


def test_cancel_verdict_dispatches_task_endpoint(monkeypatch) -> None:
    _patch_key(monkeypatch)
    calls: list[str] = []
    resp = _Resp(200, {"status": "cancelled", "verdict_jobs_cancelled": 1})
    monkeypatch.setattr(cancel_mod.httpx, "Client", lambda **kw: _Client(resp, calls))

    result = runner.invoke(
        app,
        ["cancel", "tsk", "--verdict", "--force", "--api", "http://api.test", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["http://api.test/tasks/tsk/verdict/cancel"]
    assert json.loads(result.stdout)["verdict_jobs_cancelled"] == 1


def test_cancel_analysis_dispatches_trial_endpoint(monkeypatch) -> None:
    _patch_key(monkeypatch)
    calls: list[str] = []
    resp = _Resp(200, {"status": "cancelled", "analysis_jobs_cancelled": 1})
    monkeypatch.setattr(cancel_mod.httpx, "Client", lambda **kw: _Client(resp, calls))
    monkeypatch.setattr(
        cancel_mod,
        "get_task_summary",
        lambda _api, task_id: {"id": task_id, "trials": [{"id": "tsk-0"}]},
    )

    result = runner.invoke(
        app,
        ["cancel", "tsk-0", "--analysis", "--force", "--api", "http://api.test", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["http://api.test/trials/tsk-0/analysis/cancel"]


def test_delete_trial_json_reports_records(monkeypatch) -> None:
    _patch_key(monkeypatch)

    class _DelClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def delete(self, url: str, **kw) -> httpx.Response:
            return httpx.Response(200, json={"s3_keys_deleted": 3})

    monkeypatch.setattr(delete_mod.httpx, "Client", lambda **kw: _DelClient())

    result = runner.invoke(
        app,
        [
            "delete",
            "--trial",
            "t-0",
            "--trial",
            "t-1",
            "-u",
            "http://api.test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["deleted"] == 2
    assert data["failed"] == 0
