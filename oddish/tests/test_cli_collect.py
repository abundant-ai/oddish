from __future__ import annotations

import sys
from pathlib import Path

import httpx
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app
from oddish.cli.collect import _build_payload, _guard_sources


def test_build_payload_tasks_and_trials():
    payload = _build_payload(
        name="c", tasks=["taskA", "taskA"], trial_ids=["t1"], experiments=[]
    )
    assert payload == {
        "name": "c",
        "task_ids": ["taskA"],
        "trial_ids": ["t1"],
        "experiment_ids": [],
    }


def test_guard_requires_a_source():
    assert _guard_sources(tasks=[], trial_ids=[], experiments=[]) is False
    assert _guard_sources(tasks=["a"], trial_ids=[], experiments=[]) is True


def test_build_payload_includes_experiment_ids():
    payload = _build_payload(
        name="c", tasks=["t1"], trial_ids=["tr1"], experiments=[" e1 ", "e1", "e2"]
    )
    assert payload["experiment_ids"] == ["e1", "e2"]
    assert payload["task_ids"] == ["t1"]
    assert payload["trial_ids"] == ["tr1"]


def test_guard_sources_true_with_only_experiments():
    assert _guard_sources(tasks=[], trial_ids=[], experiments=["e1"]) is True
    assert _guard_sources(tasks=[], trial_ids=[], experiments=[]) is False


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
