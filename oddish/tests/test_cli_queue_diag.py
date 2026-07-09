"""Tests for ``oddish status --queue`` queue/worker diagnostics.

Fakes the HTTP client so the CLI is exercised without a live server: asserts the
right ``/admin/*`` endpoints are called, that a 404 on the hosted-only
``worker-jobs`` endpoint is tolerated, and that an auth rejection produces a
non-zero exit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


def _make_fake_client(routes: dict[str, tuple[int, dict]], calls: list[str]):
    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

        @property
        def text(self):
            return json.dumps(self._payload)

    class _FakeClient:
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
                    return _FakeResponse(status_code, payload)
            calls.append(url)
            return _FakeResponse(404, {})

    return _FakeClient


def _invoke(routes: dict[str, tuple[int, dict]], extra: list[str] | None = None):
    import importlib

    status_module = importlib.import_module("oddish.cli.status")
    calls: list[str] = []
    fake_client = _make_fake_client(routes, calls)

    app = typer.Typer()
    app.command()(status_module.status)
    runner = CliRunner()
    with patch("oddish.cli.queue_diag.httpx.Client", fake_client):
        with patch("oddish.cli.status.require_api_key"):
            with patch("oddish.cli.status.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.queue_diag.get_auth_headers", return_value={}):
                    result = runner.invoke(app, ["--queue", *(extra or [])])
    return result, calls


_HEALTH = {
    "totals_queued": 1,
    "totals_running": 2,
    "throughput": [],
    "capacity": [
        {
            "queue_key": "openai/gpt-5.3-codex",
            "queued": 1,
            "queued_scheduled": 0,
            "running": 1,
            "limit": 8,
            "fill": 0.125,
            "oldest_queued_age_seconds": 60.0,
        }
    ],
    "dispatcher": {"component": "dispatcher", "updated_at": None},
    "reconciler": None,
    "timestamp": "2026-01-01T00:00:00+00:00",
}
_SLOTS = {"queue_keys": [], "total_slots": 0, "total_active": 0, "timestamp": "t"}
_ORPHANED = {
    "counts": {"running_stale_heartbeat": 0, "active_tasks_without_active_trials": 0},
    "trial_samples": [],
    "task_samples": [],
    "stale_after_minutes": 15,
    "timestamp": "t",
}
_STATUS = {
    "queues": [
        {"kind": "TRIAL", "queue_key": "openai/gpt-5.3-codex", "queued": 1, "running": 1},
        {"kind": "QA", "queue_key": "qa", "queued": 2, "running": 0},
    ],
    "trial_queues": [],
    "analysis_queued": 0,
    "analysis_running": 0,
    "verdict_queued": 0,
    "verdict_running": 0,
    "timestamp": "t",
}


def test_queue_diag_hits_all_admin_endpoints_and_tolerates_worker_jobs_404():
    routes = {
        "/admin/queue-health": (200, _HEALTH),
        "/admin/queue-status": (200, _STATUS),
        "/admin/slots": (200, _SLOTS),
        "/admin/orphaned-state": (200, _ORPHANED),
        "/admin/worker-jobs": (404, {}),  # hosted-only; absent on core server
    }
    result, calls = _invoke(routes)
    assert result.exit_code == 0, result.output
    for path in (
        "/admin/queue-health",
        "/admin/queue-status",
        "/admin/slots",
        "/admin/orphaned-state",
        "/admin/worker-jobs",
    ):
        assert path in calls
    # Rendered capacity should include the queue key (may be width-truncated).
    assert "Capacity by queue" in result.output
    assert "openai/gpt" in result.output
    # queue-status is rendered in human mode (per-kind rollup), not just --json.
    assert "Jobs by kind" in result.output
    assert "QA" in result.output


def test_queue_diag_partial_error_exits_nonzero_human():
    # One endpoint errors, others succeed: must surface the partial failure and
    # exit non-zero rather than silently dropping the failed section.
    routes = {
        "/admin/queue-health": (200, _HEALTH),
        "/admin/queue-status": (200, _STATUS),
        "/admin/slots": (500, {"detail": "boom"}),
        "/admin/orphaned-state": (200, _ORPHANED),
        "/admin/worker-jobs": (404, {}),
    }
    result, _ = _invoke(routes)
    assert result.exit_code == 1
    assert "Some queue diagnostics could not be fetched" in result.output


def test_queue_diag_partial_error_exits_nonzero_json():
    routes = {
        "/admin/queue-health": (200, _HEALTH),
        "/admin/queue-status": (200, _STATUS),
        "/admin/slots": (500, {"detail": "boom"}),
        "/admin/orphaned-state": (200, _ORPHANED),
        "/admin/worker-jobs": (404, {}),
    }
    result, _ = _invoke(routes, ["--json"])
    assert result.exit_code == 1


def test_queue_diag_json_emits_combined_document():
    routes = {
        "/admin/queue-health": (200, _HEALTH),
        "/admin/queue-status": (200, _STATUS),
        "/admin/slots": (200, _SLOTS),
        "/admin/orphaned-state": (200, _ORPHANED),
        "/admin/worker-jobs": (404, {}),
    }
    result, _ = _invoke(routes, ["--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert set(doc) == {
        "queue_health",
        "queue_status",
        "slots",
        "orphaned_state",
        "worker_jobs",
    }
    assert doc["worker_jobs"] is None  # 404 -> null, not an error
    assert doc["queue_health"]["totals_running"] == 2


def test_queue_diag_total_failure_exits_nonzero_human():
    # Every endpoint errors (non-auth): must not look like a healthy empty queue.
    routes = {
        "/admin/queue-health": (500, {"detail": "boom"}),
        "/admin/queue-status": (500, {"detail": "boom"}),
        "/admin/slots": (500, {"detail": "boom"}),
        "/admin/orphaned-state": (500, {"detail": "boom"}),
        "/admin/worker-jobs": (500, {"detail": "boom"}),
    }
    result, _ = _invoke(routes)
    assert result.exit_code == 1
    assert "Could not fetch any queue diagnostics" in result.output


def test_queue_diag_total_failure_exits_nonzero_json():
    routes = {
        "/admin/queue-health": (500, {"detail": "boom"}),
        "/admin/queue-status": (500, {"detail": "boom"}),
        "/admin/slots": (500, {"detail": "boom"}),
        "/admin/orphaned-state": (500, {"detail": "boom"}),
        "/admin/worker-jobs": (500, {"detail": "boom"}),
    }
    result, _ = _invoke(routes, ["--json"])
    assert result.exit_code == 1


def test_queue_diag_auth_error_exits_nonzero():
    routes = {
        "/admin/queue-health": (403, {"detail": "Admin role required"}),
        "/admin/queue-status": (403, {"detail": "Admin role required"}),
        "/admin/slots": (403, {"detail": "Admin role required"}),
        "/admin/orphaned-state": (403, {"detail": "Admin role required"}),
        "/admin/worker-jobs": (403, {"detail": "Admin role required"}),
    }
    result, _ = _invoke(routes)
    assert result.exit_code == 1
    assert "full-scope API key" in result.output


def test_queue_diag_stale_after_forwarded():
    captured_params: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return _ORPHANED

        @property
        def text(self):
            return "{}"

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            if url.endswith("/admin/orphaned-state"):
                captured_params.append(dict(params or {}))
                return _Resp()
            # Everything else: minimal valid empties.
            if url.endswith("/admin/queue-health"):
                return _Health()
            return _Empty()

    class _Health:
        status_code = 200

        def json(self):
            return _HEALTH

        @property
        def text(self):
            return "{}"

    class _Empty:
        status_code = 404

        def json(self):
            return {}

        @property
        def text(self):
            return "{}"

    import importlib

    status_module = importlib.import_module("oddish.cli.status")
    app = typer.Typer()
    app.command()(status_module.status)
    runner = CliRunner()
    with patch("oddish.cli.queue_diag.httpx.Client", _Client):
        with patch("oddish.cli.status.require_api_key"):
            with patch("oddish.cli.status.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.queue_diag.get_auth_headers", return_value={}):
                    result = runner.invoke(app, ["--queue", "--stale-after", "42"])
    assert result.exit_code == 0, result.output
    assert captured_params
    assert captured_params[0].get("stale_after_minutes") == 42
