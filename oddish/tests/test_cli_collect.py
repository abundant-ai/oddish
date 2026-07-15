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
    _resolve_task_id,
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
    # Version ids are built as {task_id}-v{int}, so a leading zero must
    # normalize or the pin silently matches nothing.
    assert _parse_task_ref("mytask@016") == ("mytask", "16")
    assert _parse_task_ref("mytask@v016") == ("mytask", "16")
    assert _parse_task_ref("mytask@0") == ("mytask", "0")


class _VersionResolveClient:
    """GET /tasks/{id}/trials returns three v16 + one v22 trial (one superseded,
    one probe, one running) so only terminal, non-probe, non-superseded v16
    trials are linked.

    The server already filters superseded rows out of this response, so the
    superseded entry below is belt-and-braces: it proves the client stays
    correct on its own if that ever changes.
    """

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


class _StatusClient:
    """Returns a fixed status for every GET."""

    def __init__(self, status):
        self.status = status
        self.urls = []

    def get(self, url, params=None):
        self.urls.append(url)
        return httpx.Response(self.status, json={})


def test_version_trial_ids_returns_none_on_fetch_error():
    # None (couldn't tell) must not be confused with [] (genuinely empty), or a
    # 401/500 silently drops trials instead of failing the collect.
    assert _version_trial_ids(_StatusClient(500), "https://api", "TID", "16") is None
    assert _version_trial_ids(_StatusClient(401), "https://api", "TID", "16") is None


class _ResolveClient:
    """GET /tasks/{ref} 200s only for known ids; browse matches on name only,
    mirroring the real endpoint (which never matches a task id)."""

    def __init__(self, ids=(), items=()):
        self.ids = set(ids)
        self.items = list(items)

    def get(self, url, params=None):
        if url.endswith("/tasks/browse"):
            needle = (params or {}).get("query", "")
            hits = [it for it in self.items if needle in it["name"]]
            return httpx.Response(200, json={"items": hits})
        ref = url.rsplit("/tasks/", 1)[-1]
        if ref in self.ids:
            return httpx.Response(200, json={"id": ref, "name": ref.rsplit("-", 1)[0]})
        return httpx.Response(404, json={})


def test_resolve_task_id_accepts_an_exact_task_id():
    # Regression: browse never matches on id, so a pinned `<task-id>@N` used to
    # fail resolution even though a bare `--task <task-id>` worked server-side.
    client = _ResolveClient(ids={"mytask-9aa07749"})
    assert _resolve_task_id(client, "https://api", "mytask-9aa07749") == "mytask-9aa07749"


def test_resolve_task_id_matches_exact_name_via_browse():
    client = _ResolveClient(items=[{"id": "mytask-9aa07749", "name": "mytask"}])
    assert _resolve_task_id(client, "https://api", "mytask") == "mytask-9aa07749"


def test_resolve_task_id_does_not_prefix_match():
    # `test` must not silently resolve to `test-harness-...`.
    client = _ResolveClient(
        items=[{"id": "test-harness-abc12345", "name": "test-harness"}]
    )
    assert _resolve_task_id(client, "https://api", "test") is None


class _PagedBrowseClient:
    """browse substring-matches and pages; the exact name sits on page 2."""

    def __init__(self, items):
        self.items = list(items)
        self.pages = 0

    def get(self, url, params=None):
        if not url.endswith("/tasks/browse"):
            return httpx.Response(404, json={})
        self.pages += 1
        p = params or {}
        needle, limit, offset = p.get("query", ""), p.get("limit", 100), p.get("offset", 0)
        hits = [it for it in self.items if needle in it["name"]]
        return httpx.Response(200, json={"items": hits[offset : offset + limit]})


def test_resolve_task_id_pages_past_the_first_browse_page():
    # An exact name outside page 1 must still resolve; a bare --task would.
    filler = [{"id": f"mytask-extra-{i}", "name": f"mytask-extra-{i}"} for i in range(150)]
    exact = {"id": "mytask-9aa07749", "name": "mytask"}
    client = _PagedBrowseClient([*filler, exact])

    assert _resolve_task_id(client, "https://api", "mytask") == "mytask-9aa07749"
    assert client.pages > 1  # proves it actually paged


def test_resolve_task_id_stops_on_a_short_page():
    client = _PagedBrowseClient([{"id": "other-abc12345", "name": "other"}])
    assert _resolve_task_id(client, "https://api", "nope") is None
    assert client.pages == 1  # short page -> no needless second request


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


class _EmptyPinClient:
    """A resolvable task whose pinned version has no linkable trials."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        if url.endswith("/tasks/mytask"):
            return httpx.Response(200, json={"id": "mytask-abc12345"})
        if url.endswith("/trials"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected url {url}")

    def post(self, url, json=None):
        raise AssertionError(f"must not post an empty collection; got {url}")


def test_collect_with_only_empty_pins_fails_locally_without_posting(monkeypatch):
    # The initial guard runs before pins are expanded, so this used to post an
    # empty payload and surface the server's 400 instead of a clear local error.
    monkeypatch.setattr(httpx, "Client", _EmptyPinClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--task", "mytask@16"])

    assert result.exit_code == 1, result.output
    assert "Nothing to collect" in result.output
