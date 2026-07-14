from __future__ import annotations

import sys
from pathlib import Path

import httpx
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app
from oddish.cli.collect import (
    _build_payload,
    _guard_sources,
    _parse_task_ref,
    _version_trial_ids,
)


def test_build_payload_tasks_and_trials():
    payload = _build_payload(name="c", tasks=["taskA", "taskA"], trial_ids=["t1"])
    assert payload == {"name": "c", "task_ids": ["taskA"], "trial_ids": ["t1"]}


def test_guard_requires_a_source():
    assert _guard_sources(tasks=[], trial_ids=[]) is False
    assert _guard_sources(tasks=["a"], trial_ids=[]) is True


def test_parse_task_ref():
    assert _parse_task_ref("mytask") == ("mytask", None)
    assert _parse_task_ref("mytask@16") == ("mytask", "16")
    assert _parse_task_ref("mytask@v16") == ("mytask", "16")
    assert _parse_task_ref("  mytask @ 16 ".replace(" ", "")) == ("mytask", "16")
    # A non-numeric suffix (e.g. an email-ish id) is not a version.
    assert _parse_task_ref("user@example.com") == ("user@example.com", None)
    # Task ids with a trailing hash but no @ are untouched.
    assert _parse_task_ref("mytask-9aa07749") == ("mytask-9aa07749", None)


class _VersionResolveClient:
    """GET /tasks/{id}/trials returns three v16 + one v22 trial (one superseded,
    one probe, one running) so only terminal, non-probe, non-superseded v16
    trials are linked."""

    def get(self, url, params=None):
        trials = [
            {"id": "T-16a", "task_version_id": "TID-v16", "status": "success",
             "is_probe": False, "superseded_by_trial_id": None},
            {"id": "T-16b", "task_version_id": "TID-v16", "status": "failed",
             "is_probe": False, "superseded_by_trial_id": None},
            {"id": "T-16-probe", "task_version_id": "TID-v16", "status": "success",
             "is_probe": True, "superseded_by_trial_id": None},
            {"id": "T-16-superseded", "task_version_id": "TID-v16",
             "status": "success", "is_probe": False,
             "superseded_by_trial_id": "T-16a"},
            {"id": "T-16-running", "task_version_id": "TID-v16", "status": "running",
             "is_probe": False, "superseded_by_trial_id": None},
            {"id": "T-22a", "task_version_id": "TID-v22", "status": "success",
             "is_probe": False, "superseded_by_trial_id": None},
        ]
        return httpx.Response(200, json=trials)


def test_version_trial_ids_filters_terminal_nonprobe_nonsuperseded():
    ids = _version_trial_ids(_VersionResolveClient(), "https://api", "TID", "16")
    assert ids == ["T-16a", "T-16b"]


# ---------------------------------------------------------------------------
# Command flow (httpx mocked) — publish orchestration
# ---------------------------------------------------------------------------

_COLLECTION_JSON = {
    "id": "c1",
    "name": "c",
    "trials_linked": 2,
    "trials_from_tasks": 2,
    "tasks_skipped_empty": 0,
}


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


class _PublishForbiddenClient:
    """create -> 200, publish -> 403 (TASKS-scoped key can't publish)."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        if url.endswith("/experiments/collections"):
            return httpx.Response(200, json=_COLLECTION_JSON)
        if url.endswith("/publish"):
            return httpx.Response(403, json={"detail": "forbidden"})
        raise AssertionError(f"unexpected url {url}")


class _NoPublishExpectedClient:
    """create -> 200; publish must NOT be called when --no-publish is given."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        if url.endswith("/experiments/collections"):
            return httpx.Response(200, json=_COLLECTION_JSON)
        raise AssertionError(f"publish must not be called with --no-publish; got {url}")


def test_collect_exits_nonzero_when_default_publish_fails(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _PublishForbiddenClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--task", "mytask"])

    assert result.exit_code == 1, result.output
    assert "NOT published" in result.output
    assert "FULL-scope" in result.output  # 403 hint


def test_collect_no_publish_exits_zero_and_skips_publish(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _NoPublishExpectedClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--task", "mytask", "--no-publish"])

    assert result.exit_code == 0, result.output
