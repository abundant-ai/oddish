from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


_MUTATION_JSON = {
    "id": "coll123",
    "name": "my collection",
    "trials_added": 2,
    "trials_removed": 0,
    "trials_total": 5,
    "tasks_linked": 1,
    "tasks_unlinked": 0,
}


class _AddClient:
    """Records the add payload and answers the share lookup."""

    posted: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        if url.endswith("/share"):
            return httpx.Response(
                200, json={"name": "my collection", "is_public": True,
                           "public_token": "tok123"}
            )
        raise AssertionError(f"unexpected get {url}")

    def post(self, url, json=None):
        type(self).posted = {"url": url, "json": json}
        return httpx.Response(200, json=_MUTATION_JSON)


def test_add_posts_trials_and_tasks(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _AddClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["experiment", "add", "coll123", "t1", "t2", "--task", "mytask",
         "--from", "exp999"],
    )

    assert result.exit_code == 0, result.output
    assert _AddClient.posted["url"].endswith("/experiments/coll123/collection/trials")
    assert _AddClient.posted["json"] == {
        "trial_ids": ["t1", "t2"],
        "task_ids": ["mytask"],
        "from_experiment_ids": ["exp999"],
    }
    assert "Trials added:" in result.output
    assert "tok123" in result.output


class _PinnedAddClient:
    """`--task mytask@16` must expand client-side into explicit trial ids."""

    posted: dict = {}

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
            return httpx.Response(
                200,
                json=[
                    {"id": "T-16a", "task_version_id": "mytask-abc12345-v16",
                     "status": "success", "is_probe": False,
                     "superseded_by_trial_id": None},
                    {"id": "T-22a", "task_version_id": "mytask-abc12345-v22",
                     "status": "success", "is_probe": False,
                     "superseded_by_trial_id": None},
                ],
            )
        if url.endswith("/share"):
            return httpx.Response(200, json={"name": "c", "is_public": False,
                                             "public_token": None})
        raise AssertionError(f"unexpected get {url}")

    def post(self, url, json=None):
        type(self).posted = {"url": url, "json": json}
        return httpx.Response(200, json=_MUTATION_JSON)


def test_add_expands_version_pin_client_side(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _PinnedAddClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["experiment", "add", "coll123", "--task", "mytask@16"]
    )

    assert result.exit_code == 0, result.output
    assert _PinnedAddClient.posted["json"]["trial_ids"] == ["T-16a"]
    assert _PinnedAddClient.posted["json"]["task_ids"] == []


class _NotACollectionClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        return httpx.Response(
            409, json={"detail": "experiment exp1 is not a collection"}
        )


def test_add_explains_409(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _NotACollectionClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["experiment", "add", "exp1", "t1"])

    assert result.exit_code == 1, result.output
    assert "not a collection" in result.output
    assert "oddish collect" in result.output  # points at the create path


def test_add_requires_a_source(monkeypatch):
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["experiment", "add", "coll123"])

    assert result.exit_code == 1, result.output
    assert "at least one" in result.output


class _ForbiddenAddClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        return httpx.Response(403, json={"detail": "insufficient scope"})


def test_add_explains_403_scope(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _ForbiddenAddClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["experiment", "add", "coll123", "t1"])

    assert result.exit_code == 1, result.output
    assert "FULL-scope" in result.output
    assert "insufficient scope" in result.output


class _ZeroTrialPinAddClient:
    """`--task mytask@16` resolves to a real task with no linkable trials for
    that version -- payload ends up empty and the CLI must not hit /collection/trials."""

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
        raise AssertionError(f"unexpected get {url}")

    def post(self, url, json=None):
        raise AssertionError("must not post when there is nothing to add")


def test_add_nothing_to_add_does_not_hit_network(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _ZeroTrialPinAddClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["experiment", "add", "coll123", "--task", "mytask@16"]
    )

    assert result.exit_code != 0, result.output
    assert "Nothing to add" in result.output


class _BadShareBodyClient:
    """The mutation succeeds (200), but the share-status GET returns a 200
    with a body that is not valid JSON. This must not crash the command --
    the mutation already committed server-side."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        if url.endswith("/share"):
            return httpx.Response(200, text="not json")
        raise AssertionError(f"unexpected get {url}")

    def post(self, url, json=None):
        return httpx.Response(200, json=_MUTATION_JSON)


def test_add_survives_non_json_share_response(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _BadShareBodyClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["experiment", "add", "coll123", "t1"])

    assert result.exit_code == 0, result.output
    assert "Trials added:" in result.output
    assert "/experiments/coll123" in result.output  # falls back to dashboard URL


_REMOVE_JSON = {
    "id": "coll123",
    "name": "my collection",
    "trials_added": 0,
    "trials_removed": 3,
    "trials_total": 2,
    "tasks_linked": 0,
    "tasks_unlinked": 1,
}


class _RemoveClient:
    requested: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        if url.endswith("/share"):
            return httpx.Response(200, json={"name": "c", "is_public": False,
                                             "public_token": None})
        raise AssertionError(f"unexpected get {url}")

    def request(self, method, url, json=None):
        type(self).requested = {"method": method, "url": url, "json": json}
        return httpx.Response(200, json=_REMOVE_JSON)


def test_remove_with_yes_issues_delete(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _RemoveClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["experiment", "remove", "coll123", "--task", "mytask", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert _RemoveClient.requested["method"] == "DELETE"
    assert _RemoveClient.requested["url"].endswith(
        "/experiments/coll123/collection/trials"
    )
    assert _RemoveClient.requested["json"] == {
        "trial_ids": [],
        "task_ids": ["mytask"],
    }
    assert "Trials removed: 3" in result.output


class _MustNotRequestClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, json=None):
        raise AssertionError("must not issue a request when the user declines")

    def get(self, url, params=None):
        raise AssertionError("must not issue a request when the user declines")


def test_remove_aborts_without_confirmation(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _MustNotRequestClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["experiment", "remove", "coll123", "t1"], input="n\n"
    )

    assert result.exit_code == 1, result.output
    assert "Aborted" in result.output


class _EmptyingClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, json=None):
        return httpx.Response(
            409, json={"detail": "removing these trials would empty the collection"}
        )


def test_remove_explains_emptying_409(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _EmptyingClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["experiment", "remove", "coll123", "t1", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert "would empty the collection" in result.output


class _ForbiddenRemoveClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, json=None):
        return httpx.Response(403, json={"detail": "insufficient scope"})


def test_remove_explains_403_scope(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _ForbiddenRemoveClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["experiment", "remove", "coll123", "t1", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert "FULL-scope" in result.output


class _ZeroTrialPinRemoveClient:
    """`--task mytask@16` resolves to a real task with no linkable trials for
    that version -- payload ends up empty and the CLI must not issue the DELETE."""

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
        raise AssertionError(f"unexpected get {url}")

    def request(self, method, url, json=None):
        raise AssertionError("must not issue a request when there is nothing to remove")


def test_remove_nothing_to_remove_does_not_hit_network(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _ZeroTrialPinRemoveClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["experiment", "remove", "coll123", "--task", "mytask@16", "--yes"]
    )

    assert result.exit_code != 0, result.output
    assert "Nothing to remove" in result.output
