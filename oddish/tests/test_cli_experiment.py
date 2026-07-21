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
