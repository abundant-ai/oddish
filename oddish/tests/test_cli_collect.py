from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import typer
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


def _fake_browse_hits(query: str, items: list[dict]) -> list[dict]:
    """Mimic parse_search_query + the name ILIKE: a quoted phrase is one
    literal term, while a bare leading ``-`` makes the term an *exclusion*.
    Without that second rule the fake would match a `-name` needle happily and
    the quoting regression below would pass against unquoted code too."""
    q = query.strip()
    if len(q) >= 2 and q.startswith('"') and q.endswith('"'):
        needle, negated = q[1:-1], False
    elif q.startswith("-"):
        needle, negated = q[1:], True
    else:
        needle, negated = q, False
    if negated:
        return [it for it in items if needle not in it["name"]]
    return [it for it in items if needle in it["name"]]


class _ResolveClient:
    """GET /tasks/{ref} 200s only for known ids; browse matches on name only,
    mirroring the real endpoint (which never matches a task id)."""

    def __init__(self, ids=(), items=(), id_status=404):
        self.ids = set(ids)
        self.items = list(items)
        self.id_status = id_status

    def get(self, url, params=None):
        if url.endswith("/tasks/browse"):
            hits = _fake_browse_hits((params or {}).get("query", ""), self.items)
            return httpx.Response(200, json={"items": hits})
        ref = url.rsplit("/tasks/", 1)[-1]
        if ref in self.ids:
            return httpx.Response(200, json={"id": ref, "name": ref.rsplit("-", 1)[0]})
        return httpx.Response(self.id_status, json={})


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
        limit, offset = p.get("limit", 100), p.get("offset", 0)
        hits = _fake_browse_hits(p.get("query", ""), self.items)
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


def test_resolve_task_id_aborts_on_id_lookup_http_error():
    # A 401/500 must not be reported as "task not found" -- same
    # error-vs-empty misclassification as _version_trial_ids had.
    for status in (401, 500):
        client = _ResolveClient(id_status=status)
        with pytest.raises(typer.Exit):
            _resolve_task_id(client, "https://api", "mytask")


class _BrowseErrorClient:
    """id lookup 404s (not an id); the name search then fails hard."""

    def get(self, url, params=None):
        if url.endswith("/tasks/browse"):
            return httpx.Response(503, json={})
        return httpx.Response(404, json={})


def test_resolve_task_id_aborts_on_browse_http_error():
    with pytest.raises(typer.Exit):
        _resolve_task_id(_BrowseErrorClient(), "https://api", "mytask")


def test_resolve_task_id_quotes_the_needle_for_leading_dash_names():
    # browse runs the needle through parse_search_query, where a leading `-`
    # is an exclusion; quoting keeps it a literal.
    client = _ResolveClient(items=[{"id": "-weird-abc12345", "name": "-weird"}])
    assert _resolve_task_id(client, "https://api", "-weird") == "-weird-abc12345"


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


class _RecordingCreateClient:
    """Records the create + publish calls so the create path can be pinned down."""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        type(self).calls.append(("POST", url, json))
        if url.endswith("/experiments/collections"):
            return httpx.Response(200, json=_COLLECTION_JSON)
        if url.endswith("/publish"):
            return httpx.Response(200, json={"public_token": "tok"})
        raise AssertionError(f"unexpected url {url}")


def test_collect_create_path_is_unchanged_by_into(monkeypatch):
    # Adding --into must not perturb the default create+publish flow.
    _RecordingCreateClient.calls = []
    monkeypatch.setattr(httpx, "Client", _RecordingCreateClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--task", "mytask", "-n", "roll"])

    assert result.exit_code == 0, result.output
    assert [c[1] for c in _RecordingCreateClient.calls] == [
        "https://api.example.test/experiments/collections",
        "https://api.example.test/experiments/c1/publish",
    ]
    assert _RecordingCreateClient.calls[0][2] == {
        "name": "roll",
        "task_ids": ["mytask"],
        "trial_ids": [],
    }
    assert "Created collection c1" in result.output
    assert "/share/tok" in result.output


# ---------------------------------------------------------------------------
# --into: append / rename an existing collection
# ---------------------------------------------------------------------------

# CollectionMutationResponse serializes every field, so the rename response
# really does carry zeroed counters that must not clobber the append's.
_ADD_JSON = {
    "id": "c1",
    "name": "old name",
    "trials_added": 3,
    "trials_removed": 0,
    "trials_total": 7,
    "tasks_linked": 1,
    "tasks_unlinked": 0,
    "trials_skipped": 0,
}
_RENAME_JSON = {
    "id": "c1",
    "name": "new name",
    "trials_added": 0,
    "trials_removed": 0,
    "trials_total": 0,
    "tasks_linked": 0,
    "tasks_unlinked": 0,
    "trials_skipped": 0,
}


def _mutate_client(*, rename_status=200, add_status=200, pin_trials=None):
    """Fake httpx.Client for --into mode, plus the list it records calls into."""
    calls: list = []

    class _C:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            calls.append(("GET", url, params))
            if url.endswith("/tasks/mytask"):
                return httpx.Response(200, json={"id": "mytask-abc12345"})
            if url.endswith("/trials"):
                return httpx.Response(200, json=pin_trials or [])
            raise AssertionError(f"unexpected url {url}")

        def post(self, url, json=None):
            calls.append(("POST", url, json))
            if url.endswith("/collection/trials"):
                return httpx.Response(
                    add_status, json=_ADD_JSON if add_status == 200 else {}
                )
            raise AssertionError(f"unexpected post {url}")

        def patch(self, url, json=None):
            calls.append(("PATCH", url, json))
            return httpx.Response(
                rename_status, json=_RENAME_JSON if rename_status == 200 else {}
            )

    return _C, calls


def test_collect_into_rename_only(monkeypatch):
    client, calls = _mutate_client()
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--into", "c1", "-n", "new name"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "PATCH",
            "https://api.example.test/experiments/c1/collection",
            {"name": "new name"},
        )
    ]
    assert "Updated collection c1" in result.output
    assert "Renamed to" in result.output


def test_collect_into_append_only(monkeypatch):
    client, calls = _mutate_client()
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["collect", "--into", "c1", "--task", "mytask", "trial-a"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            "https://api.example.test/experiments/c1/collection/trials",
            {
                "trial_ids": ["trial-a"],
                "task_ids": ["mytask"],
                "from_experiment_ids": [],
            },
        )
    ]
    assert "Trials added:       3" in result.output


def test_collect_into_append_and_rename(monkeypatch):
    client, calls = _mutate_client()
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["collect", "--into", "c1", "--task", "mytask", "-n", "new name"]
    )

    assert result.exit_code == 0, result.output
    # Append first: it needs only TASKS, so a non-admin key still lands trials.
    assert [c[0] for c in calls] == ["POST", "PATCH"]
    assert "Trials added:       3" in result.output
    assert "new name" in result.output


def test_collect_into_never_publishes(monkeypatch):
    # publish defaults to True; --into must not fire it (the collection exists
    # and may already be shared).
    client, calls = _mutate_client()
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--into", "c1", "--task", "mytask"])

    assert result.exit_code == 0, result.output
    assert not any("/publish" in c[1] for c in calls)


def test_collect_into_without_name_or_sources_errors(monkeypatch):
    client, calls = _mutate_client()
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--into", "c1"])

    assert result.exit_code == 1, result.output
    assert "Nothing to do" in result.output
    assert calls == []


def test_collect_into_rename_403_explains_the_scope(monkeypatch):
    client, _ = _mutate_client(rename_status=403)
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--into", "c1", "-n", "new name"])

    assert result.exit_code == 1, result.output
    assert "Rename failed" in result.output
    assert "admin API key" in result.output


def test_collect_into_expands_version_pins(monkeypatch):
    client, calls = _mutate_client(
        pin_trials=[
            {
                "id": "T-16a",
                "task_version_id": "mytask-abc12345-v16",
                "status": "success",
                "is_probe": False,
                "superseded_by_trial_id": None,
            }
        ]
    )
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--into", "c1", "--task", "mytask@16"])

    assert result.exit_code == 0, result.output
    post = [c for c in calls if c[0] == "POST"][0]
    assert post[2] == {
        "trial_ids": ["T-16a"],
        "task_ids": [],
        "from_experiment_ids": [],
    }


def test_collect_into_with_only_empty_pins_fails_locally(monkeypatch):
    client, calls = _mutate_client()
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["collect", "--into", "c1", "--task", "mytask@16"])

    assert result.exit_code == 1, result.output
    assert "Nothing to append" in result.output
    assert not any(c[0] in ("POST", "PATCH") for c in calls)


def test_collect_into_json_output(monkeypatch):
    client, _ = _mutate_client()
    monkeypatch.setattr(httpx, "Client", client)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["collect", "--into", "c1", "--task", "mytask", "-n", "new name", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Append's counters survive; only the name comes from the rename response.
    assert payload["trials_added"] == 3
    assert payload["name"] == "new name"
