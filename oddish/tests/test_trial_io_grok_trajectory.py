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

    async def object_exists(self, _key: str) -> bool:
        return False

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
        attempts=1,
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


def test_exact_attempt_converts_only_its_selected_grok_build_artifact(monkeypatch):
    prefix = "tasks/task-1/trials/trial-exact/attempt-2/"
    selected_prefix = f"{prefix}current-run/"
    manifest_key = f"{prefix}result.json"
    grok_key = f"{selected_prefix}agent/grok-build.json"
    stale_key = f"{prefix}old-run/agent/trajectory.json"
    grok_text = "\n".join(
        [
            json.dumps({"type": "thought", "data": "Inspect exact attempt. "}),
            json.dumps({"type": "text", "data": "Fixed."}),
            json.dumps({"type": "end", "sessionId": "exact-grok-session"}),
        ]
    )

    class ExactStorage:
        def __init__(self):
            self.objects = {
                manifest_key: json.dumps(
                    {"trial_results": [{"trial_name": "current-run"}]}
                ),
                grok_key: grok_text,
                stale_key: json.dumps({"session_id": "stale"}),
            }
            self.downloaded = []

        async def object_exists(self, key):
            return key in self.objects

        async def download_text(self, key):
            self.downloaded.append(key)
            return self.objects[key]

        async def list_keys(self, _prefix):
            raise AssertionError("exact reads must not scan sibling directories")

    storage = ExactStorage()
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = SimpleNamespace(
        id="trial-exact",
        name="trial-exact",
        model="xai/redacted-model",
        trial_s3_key=prefix,
        attempts=2,
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert trajectory["session_id"] == "exact-grok-session"
    assert trajectory["agent"]["name"] == "grok-build"
    assert stale_key not in storage.downloaded


def test_exact_attempt_uses_grok_build_when_trajectory_json_is_malformed(monkeypatch):
    prefix = "tasks/task-1/trials/trial-fallback/attempt-2/"
    selected_prefix = f"{prefix}current-run/"
    manifest_key = f"{prefix}result.json"
    trajectory_key = f"{selected_prefix}agent/trajectory.json"
    grok_key = f"{selected_prefix}agent/grok-build.json"
    grok_text = "\n".join(
        [
            json.dumps({"type": "thought", "data": "Recover the evidence. "}),
            json.dumps({"type": "text", "data": "Recovered."}),
            json.dumps({"type": "end", "sessionId": "fallback-session"}),
        ]
    )

    class ExactStorage:
        def __init__(self):
            self.objects = {
                manifest_key: json.dumps(
                    {"trial_results": [{"trial_name": "current-run"}]}
                ),
                trajectory_key: "{malformed",
                grok_key: grok_text,
            }

        async def object_exists(self, key):
            return key in self.objects

        async def download_text(self, key):
            return self.objects[key]

        async def list_keys(self, _prefix):
            raise AssertionError("exact reads must not scan sibling directories")

    monkeypatch.setattr(trial_io, "get_storage_client", ExactStorage)
    trial = SimpleNamespace(
        id="trial-fallback",
        name="trial-fallback",
        model="xai/redacted-model",
        trial_s3_key=prefix,
        attempts=2,
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert trajectory["session_id"] == "fallback-session"
    assert trajectory["agent"]["name"] == "grok-build"


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
        attempts=1,
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert trajectory["steps"][0]["source"] == "user"
    assert trajectory["steps"][0]["observation"]["results"][0]["content"] == (
        "command output"
    )
