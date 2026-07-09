"""Tests for ``oddish status`` trial detail + task detail/versions views."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


def _make_client(routes: dict[str, tuple[int, object]], calls: list[str]):
    class _Resp:
        def __init__(self, status_code: int, payload: object):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

        @property
        def text(self):
            return json.dumps(self._payload)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            for path, (status_code, payload) in routes.items():
                if url.endswith(path):
                    calls.append(path)
                    return _Resp(status_code, payload)
            calls.append(url)
            return _Resp(404, {"detail": "Not Found"})

    return _Client


def _invoke(routes: dict[str, tuple[int, object]], args: list[str]):
    import importlib

    status_module = importlib.import_module("oddish.cli.status")
    calls: list[str] = []
    fake = _make_client(routes, calls)
    app = typer.Typer()
    app.command()(status_module.status)
    runner = CliRunner()
    with patch("oddish.cli.detail.httpx.Client", fake):
        with patch("oddish.cli.status.require_api_key"):
            with patch("oddish.cli.status.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.detail.get_auth_headers", return_value={}):
                    result = runner.invoke(app, args)
    return result, calls


_TRIAL = {
    "id": "task-abc-0",
    "task_id": "task-abc",
    "agent": "claude-code",
    "model": "anthropic/claude-sonnet-4-5",
    "provider": "anthropic",
    "queue_key": "anthropic/claude-sonnet-4-5",
    "status": "success",
    "harbor_stage": "completed",
    "reward": 1,
    "attempts": 1,
    "max_attempts": 6,
    "input_tokens": 15000,
    "output_tokens": 3200,
    "cost_usd": 0.12,
    "total_steps": 42,
    "has_trajectory": True,
}


def test_status_trial_id_uses_single_trial_route():
    routes = {"/trials/task-abc-0": (200, _TRIAL)}
    result, calls = _invoke(routes, ["task-abc-0", "--json"])
    assert result.exit_code == 0, result.output
    assert "/trials/task-abc-0" in calls
    assert json.loads(result.output)["id"] == "task-abc-0"


def test_status_trial_id_falls_back_to_trial_by_index_on_core():
    # Core server has no /trials/{id}: 404 -> fetch the trial by index, which
    # returns the exact trial (incl. superseded / old versions).
    routes = {
        "/trials/task-abc-0": (404, {"detail": "Not Found"}),
        "/tasks/task-abc-0": (404, {"detail": "Not Found"}),
        "/tasks/task-abc/trials/0": (200, _TRIAL),
    }
    result, calls = _invoke(routes, ["task-abc-0"])
    assert result.exit_code == 0, result.output
    assert "/tasks/task-abc/trials/0" in calls
    assert "Trial: task-abc-0" in result.output


def test_status_trial_id_with_watch_still_shows_detail():
    # --watch must not misroute a trial id to the task/experiment watch path.
    routes = {"/trials/task-abc-0": (200, _TRIAL)}
    result, calls = _invoke(routes, ["task-abc-0", "--watch"])
    assert result.exit_code == 0, result.output
    assert "/trials/task-abc-0" in calls
    assert "Trial: task-abc-0" in result.output


def test_status_trial_id_non_404_error_does_not_fall_back():
    # A 403 on /trials/{id} must surface as an error, not silently show the
    # parent task's embedded copy.
    routes = {
        "/trials/task-abc-0": (403, {"detail": "forbidden"}),
        "/tasks/task-abc": (200, {"id": "task-abc", "trials": [_TRIAL]}),
    }
    result, calls = _invoke(routes, ["task-abc-0"])
    assert result.exit_code != 0
    assert "/tasks/task-abc" not in calls


def test_status_detail_shows_zero_billed_cost():
    detail = {
        "task": {"id": "task-abc", "name": "demo", "status": "completed"},
        "totals": {
            "cost_usd": 0.5,
            "cost_trial_count": 3,
            "billed_cost_usd": 0.0,
            "billed_trial_count": 2,
        },
        "versions": [],
    }
    routes = {"/tasks/task-abc/detail": (200, detail)}
    result, _ = _invoke(routes, ["task-abc", "--detail"])
    assert result.exit_code == 0, result.output
    assert "Billed cost: $0.0000 across 2 trials" in result.output


def test_status_detail_renders_versions_and_cost():
    detail = {
        "task": {"id": "task-abc", "name": "demo", "status": "completed", "progress": "1/1 finished"},
        "totals": {"cost_usd": 0.5, "cost_trial_count": 3, "cost_has_estimated": False},
        "versions": [
            {"version": 2, "is_current": True, "trial_count": 3, "pass_count": 2, "reward_total": 3, "cost_usd": 0.5, "message": "v2"},
        ],
    }
    routes = {"/tasks/task-abc/detail": (200, detail)}
    result, calls = _invoke(routes, ["task-abc", "--detail"])
    assert result.exit_code == 0, result.output
    assert "/tasks/task-abc/detail" in calls
    assert "Total cost" in result.output
    assert "Versions" in result.output


def test_status_versions_list():
    versions = [
        {"version": 2, "created_at": "2026-01-02", "content_hash": "hash2xxxxxxx", "message": "v2"},
        {"version": 1, "created_at": "2026-01-01", "content_hash": "hash1xxxxxxx", "message": "v1"},
    ]
    routes = {"/tasks/task-abc/versions": (200, versions)}
    result, calls = _invoke(routes, ["task-abc", "--versions"])
    assert result.exit_code == 0, result.output
    assert "/tasks/task-abc/versions" in calls
    assert "hash2" in result.output


def test_status_single_version():
    version = {"version": 1, "task_id": "task-abc", "content_hash": "abc", "task_path": "demo", "created_at": "2026-01-01", "message": "first"}
    routes = {"/tasks/task-abc/versions/1": (200, version)}
    result, calls = _invoke(routes, ["task-abc", "--versions", "--version", "1"])
    assert result.exit_code == 0, result.output
    assert "/tasks/task-abc/versions/1" in calls
    assert "first" in result.output


def _call_try_trial(routes: dict[str, tuple[int, object]]):
    """Call try_print_trial_detail directly against faked routes."""
    import importlib

    detail = importlib.import_module("oddish.cli.detail")
    calls: list[str] = []
    fake = _make_client(routes, calls)
    with patch("oddish.cli.detail.httpx.Client", fake):
        with patch("oddish.cli.detail.get_auth_headers", return_value={}):
            handled = detail.try_print_trial_detail(
                "http://localhost", "abc-0", json_output=True
            )
    return handled, calls


def test_trial_detail_yields_to_real_task_with_trial_shaped_id():
    # `abc-0` is itself a task (not a trial): must NOT be shadowed by task
    # `abc`'s embedded trial 0. try_print_trial_detail returns False so normal
    # task-status handling runs.
    routes = {
        "/trials/abc-0": (404, {"detail": "Not Found"}),
        "/tasks/abc-0": (200, {"id": "abc-0", "name": "real-task", "trials": []}),
        "/tasks/abc": (200, {"id": "abc", "trials": [{"id": "abc-0"}]}),
    }
    handled, calls = _call_try_trial(routes)
    assert handled is False
    assert "/tasks/abc-0" in calls  # checked task identity before parent fallback


def test_trial_detail_core_fallback_uses_trial_by_index():
    # `abc-0` is a genuine trial on a core server (no /trials route, and no task
    # with that exact id): fall back to the by-index route (works for superseded
    # trials that a parent task's embedded list would omit).
    routes = {
        "/trials/abc-0": (404, {"detail": "Not Found"}),
        "/tasks/abc-0": (404, {"detail": "Not Found"}),
        "/tasks/abc/trials/0": (200, {"id": "abc-0", "task_id": "abc", "agent": "x", "provider": "y", "queue_key": "q", "status": "success", "attempts": 1, "max_attempts": 6}),
    }
    handled, calls = _call_try_trial(routes)
    assert handled is True
    assert "/tasks/abc/trials/0" in calls


def test_status_detail_requires_task_id():
    result, _ = _invoke({}, ["--detail"])
    assert result.exit_code != 0


def test_status_version_requires_versions_flag():
    result, _ = _invoke({}, ["task-abc", "--version", "1"])
    assert result.exit_code != 0
