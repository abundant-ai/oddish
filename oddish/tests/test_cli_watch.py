"""Completion semantics for the CLI task watcher."""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from oddish.cli import api


class _Live:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_Live":
        return self

    def __exit__(self, *args) -> None:
        pass

    def update(self, value) -> None:
        pass


class _Client:
    def __init__(self, responses: Iterator[httpx.Response], *args, **kwargs) -> None:
        self._responses = responses

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *args) -> None:
        pass

    def get(self, url: str) -> httpx.Response:
        return next(self._responses)


def _response(task_status: str) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("GET", "https://api.example.test/tasks/task-1"),
        json={
            "id": "task-1",
            "status": task_status,
            "verdict_status": "running" if task_status == "analyzing" else "success",
            "trials": [
                {
                    "id": "trial-1",
                    "agent": "codex",
                    "model": "openai/gpt-5.6",
                    "status": "success",
                    "reward": 1,
                }
            ],
        },
    )


def test_filtered_watch_keeps_existing_terminal_trial_behavior(monkeypatch) -> None:
    responses = iter([_response("analyzing")])
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setattr(api, "Live", _Live)
    monkeypatch.setattr(api.httpx, "Client", lambda *a, **kw: _Client(responses))
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)

    result = api.watch_task("https://api.example.test", "task-1", trial_ids=["trial-1"])

    assert result is not None
    assert result["status"] == "analyzing"


def test_filtered_watch_waits_for_terminal_qa_when_requested(monkeypatch) -> None:
    responses = iter([_response("analyzing"), _response("completed")])
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setattr(api, "Live", _Live)
    monkeypatch.setattr(api.httpx, "Client", lambda *a, **kw: _Client(responses))
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)

    result = api.watch_task(
        "https://api.example.test",
        "task-1",
        trial_ids=["trial-1"],
        wait_for_qa=True,
    )

    assert result is not None
    assert result["status"] == "completed"


def test_qa_wait_returns_none_on_network_failure(monkeypatch) -> None:
    class _FailingClient(_Client):
        def get(self, url: str) -> httpx.Response:
            raise httpx.ConnectError("offline")

    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setattr(api, "Live", _Live)
    monkeypatch.setattr(
        api.httpx,
        "Client",
        lambda *a, **kw: _FailingClient(iter(())),
    )
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)

    result = api.watch_task(
        "https://api.example.test",
        "task-1",
        trial_ids=["trial-1"],
        wait_for_qa=True,
    )

    assert result is None
