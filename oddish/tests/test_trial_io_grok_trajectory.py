from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from oddish.core import trial_io


class _FakeStorage:
    def __init__(self, grok_text: str):
        self.grok_text = grok_text

    async def download_text(self, key: str) -> str:
        if key.endswith("/agent/grok-build.json"):
            return self.grok_text
        raise FileNotFoundError(key)

    async def list_keys(self, prefix: str) -> list[str]:
        return []


def test_read_trial_trajectory_converts_grok_build_s3_artifact(monkeypatch):
    grok_text = "\n".join(
        [
            json.dumps({"type": "thought", "data": "Reasoning about the fix. "}),
            json.dumps({"type": "text", "data": "Done."}),
            json.dumps({"type": "end", "sessionId": "grok-session"}),
        ]
    )
    monkeypatch.setattr(
        trial_io,
        "get_storage_client",
        lambda: _FakeStorage(grok_text),
    )
    trial = SimpleNamespace(
        id="trial-1",
        name="trial-1",
        model="xai/redacted-model",
        trial_s3_key="tasks/task-1/trials/trial-1/",
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert trajectory["session_id"] == "grok-session"
    assert trajectory["agent"]["name"] == "grok-build"
    assert [step["message"] for step in trajectory["steps"]] == [
        "Reasoning",
        "Done.",
    ]


def test_read_trial_trajectory_marks_tool_results_as_user_observations(monkeypatch):
    grok_text = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "content": "command output",
                }
            ),
            json.dumps({"type": "end", "sessionId": "grok-session"}),
        ]
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: _FakeStorage(grok_text))
    trial = SimpleNamespace(
        id="trial-tool-result",
        name="trial-tool-result",
        model="xai/redacted-model",
        trial_s3_key="tasks/task-1/trials/trial-tool-result/",
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert trajectory["steps"][0]["source"] == "user"
    assert trajectory["steps"][0]["observation"]["results"][0]["content"] == (
        "command output"
    )
