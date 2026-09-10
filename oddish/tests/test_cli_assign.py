import json

import httpx
import pytest
from typer.testing import CliRunner

from oddish.cli import app


@pytest.fixture
def requests(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")
    recorded = []

    def respond(request):
        recorded.append(request)
        data = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "owner_user_id": "user-alice",
                "assigned_task_ids": data["task_ids"],
                "unchanged_task_ids": [],
                "skipped_task_ids": [],
            },
        )

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    return recorded


def test_assign_file_of_200_ids_uses_one_request_and_json_output(tmp_path, requests):
    ids = [f"task-{n}" for n in range(200)]
    path = tmp_path / "tasks.txt"
    path.write_text("\n".join(ids) + "\n\n")
    result = CliRunner().invoke(
        app,
        [
            "assign",
            "task-0",
            "--tasks-file",
            str(path),
            "--to",
            "alice@example.com",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["assigned_task_ids"] == ids
    assert len(requests) == 1
    assert requests[0].url.path == "/tasks/qa-work/assign"
    assert json.loads(requests[0].content) == {
        "task_ids": ids,
        "assignee": "alice@example.com",
        "replace": False,
    }


def test_assign_positional_ids_and_replace(requests):
    result = CliRunner().invoke(
        app, ["assign", "task-1", "task-2", "--to", "@alice", "--replace"]
    )
    assert result.exit_code == 0, result.output
    assert "Assigned 2 tasks" in result.stdout
    assert json.loads(requests[0].content)["replace"] is True


@pytest.mark.parametrize("args", [[], [f"task-{n}" for n in range(1001)]])
def test_assign_rejects_empty_or_oversized_batch_without_request(requests, args):
    result = CliRunner().invoke(app, ["assign", *args, "--to", "alice@example.com"])
    assert result.exit_code != 0
    assert requests == []


def test_assign_http_failure_is_reported_without_traceback(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    403, json={"detail": "Admin role required"}
                )
            ),
            **kw,
        ),
    )
    result = CliRunner().invoke(app, ["assign", "task-1", "--to", "alice"])
    assert result.exit_code == 1
    assert "Admin role required" in result.output


def test_assign_reports_skipped_tasks(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "owner_user_id": "alice",
                        "assigned_task_ids": [],
                        "unchanged_task_ids": [],
                        "skipped_task_ids": ["task-1"],
                    },
                )
            ),
            **kw,
        ),
    )
    result = CliRunner().invoke(app, ["assign", "task-1", "--to", "alice"])
    assert result.exit_code == 0, result.output
    assert "Skipped: task-1" in result.stdout
    assert "--replace" in result.stdout
