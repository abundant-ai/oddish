from __future__ import annotations

from io import StringIO
import importlib
from pathlib import Path
import sys
from typing import Any

import httpx
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

status_cli = importlib.import_module("oddish.cli.status")


class _FakeClient:
    def __init__(self, get_result: httpx.Response | Exception):
        self.get_result = get_result
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self.requests.append((url, params))
        if isinstance(self.get_result, Exception):
            raise self.get_result
        return self.get_result


def _capture_status_console(monkeypatch) -> StringIO:
    output = StringIO()
    monkeypatch.setattr(
        status_cli,
        "console",
        Console(file=output, color_system=None, width=120),
    )
    return output


def test_system_status_uses_dashboard_experiment_summary(monkeypatch) -> None:
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    output = _capture_status_console(monkeypatch)
    response = httpx.Response(
        200,
        json={
            "experiments": [
                {
                    "id": "exp-1",
                    "name": "Recent run",
                    "task_count": 4,
                    "active_trials": 1,
                    "total_trials": 3,
                    "completed_trials": 2,
                    "reward_success": 1,
                    "reward_total": 2,
                }
            ]
        },
    )
    client = _FakeClient(response)

    def fake_client(*, timeout: float, headers: dict[str, str]) -> _FakeClient:
        assert timeout == status_cli.SYSTEM_STATUS_TIMEOUT_SECONDS
        assert headers == {"Authorization": "Bearer ok_test"}
        return client

    monkeypatch.setattr(status_cli.httpx, "Client", fake_client)

    status_cli.status(api_url="https://api.example")

    assert client.requests == [
        (
            "https://api.example/dashboard",
            {
                "include_tasks": "false",
                "include_usage": "false",
                "include_experiments": "true",
                "experiments_limit": status_cli.RECENT_EXPERIMENTS_LIMIT,
                "experiments_offset": 0,
                "experiments_status": "active",
            },
        )
    ]
    rendered = output.getvalue()
    assert "Active Experiments" in rendered
    assert "exp-1" in rendered
    assert "Recent run" in rendered
    assert "2/3" in rendered
    assert "1/2" in rendered
    assert "Tip: oddish status --experiment <id> --watch" in rendered
    assert "All systems operational" in rendered


def test_system_status_falls_back_to_recent_when_no_active_experiments(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    output = _capture_status_console(monkeypatch)
    responses = [
        httpx.Response(200, json={"experiments": []}),
        httpx.Response(
            200,
            json={
                "experiments": [
                    {
                        "id": "exp-recent",
                        "name": "Completed run",
                        "task_count": 1,
                        "active_trials": 0,
                        "total_trials": 1,
                        "completed_trials": 1,
                        "reward_success": 1,
                        "reward_total": 1,
                    }
                ]
            },
        ),
    ]
    client = _FakeClient(responses[0])

    def fake_get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        client.requests.append((url, params))
        return responses.pop(0)

    client.get = fake_get  # type: ignore[method-assign]

    def fake_client(*, timeout: float, headers: dict[str, str]) -> _FakeClient:
        return client

    monkeypatch.setattr(status_cli.httpx, "Client", fake_client)

    status_cli.status(api_url="https://api.example")

    assert [request[1]["experiments_status"] for request in client.requests] == [
        "active",
        "all",
    ]
    rendered = output.getvalue()
    assert "No active experiments; showing recent." in rendered
    assert "exp-recent" in rendered
    assert "Completed run" in rendered
    assert "Tip: oddish status --experiment <id> --watch" in rendered


def test_system_status_reports_timeout_detail(monkeypatch) -> None:
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    output = _capture_status_console(monkeypatch)
    client = _FakeClient(httpx.ReadTimeout("read timed out"))

    def fake_client(*, timeout: float, headers: dict[str, str]) -> _FakeClient:
        return client

    monkeypatch.setattr(status_cli.httpx, "Client", fake_client)

    status_cli.status(api_url="https://api.example")

    rendered = output.getvalue()
    assert "Timed out fetching experiment status" in rendered
    assert "read timed out" in rendered
    assert "Failed to connect to API" not in rendered
    assert "1 issue(s) detected" in rendered
