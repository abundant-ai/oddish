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


class _FakeSessionStorage:
    def __init__(self, prefix: str, session_id: str):
        self.prefix = prefix
        self.session_id = session_id
        self.collapsed = json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": session_id,
                "agent": {"name": "grok-build-api-key-no-search"},
                "steps": [{"step_id": 1, "source": "agent", "message": "Final text."}],
            }
        )
        self.grok_build = json.dumps(
            {"text": "Final text.", "sessionId": session_id, "num_turns": 3}
        )
        updates = [
            {
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Investigating."},
                    }
                }
            },
            {
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "call-1",
                        "title": "run_terminal_command",
                        "rawInput": {"command": "git fsck"},
                    }
                }
            },
            {
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "call-1",
                        "content": [{"type": "text", "text": "dangling blob"}],
                    }
                }
            },
            {
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Final text."},
                    }
                }
            },
        ]
        self.updates_bytes = "\n".join(json.dumps(u) for u in updates).encode()
        self.updates_key = (
            f"{prefix}trial-x/agent/grok-session/sessions/=252Fapp/"
            f"{session_id}/updates.jsonl"
        )

    async def download_text(self, key: str) -> str:
        if key.endswith("/agent/trajectory.json"):
            return self.collapsed
        if key.endswith("/agent/grok-build.json"):
            return self.grok_build
        raise FileNotFoundError(key)

    async def download_bytes(self, key: str) -> bytes:
        if key == self.updates_key:
            return self.updates_bytes
        raise FileNotFoundError(key)

    async def list_keys(self, prefix: str) -> list[str]:
        return [self.updates_key]


def test_read_trial_trajectory_rebuilds_collapsed_from_captured_session(monkeypatch):
    prefix = "tasks/task-1/trials/trial-collapsed/"
    storage = _FakeSessionStorage(prefix, "019f-session")
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = SimpleNamespace(
        id="trial-collapsed",
        name="trial-collapsed",
        model="xai/redacted-model",
        trial_s3_key=prefix,
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert trajectory["agent"]["extra"]["trajectory_source"] == "grok_session"
    steps = trajectory["steps"]
    assert len(steps) == 3
    tool_steps = [s for s in steps if s.get("tool_calls")]
    assert tool_steps[0]["tool_calls"][0]["arguments"] == {"command": "git fsck"}
    assert tool_steps[0]["observation"]["results"][0]["content"] == "dangling blob"


def test_read_trial_trajectory_keeps_multi_step_trajectory_untouched(monkeypatch):
    prefix = "tasks/task-1/trials/trial-rich/"

    class _RichStorage(_FakeSessionStorage):
        def __init__(self):
            super().__init__(prefix, "019f-session")
            self.collapsed = json.dumps(
                {
                    "schema_version": "ATIF-v1.7",
                    "agent": {"name": "grok-build"},
                    "steps": [
                        {"step_id": 1, "source": "agent", "message": "a"},
                        {"step_id": 2, "source": "agent", "message": "b"},
                    ],
                }
            )

        async def list_keys(self, prefix: str) -> list[str]:
            raise AssertionError("rich trajectory must not trigger a rebuild")

    monkeypatch.setattr(trial_io, "get_storage_client", lambda: _RichStorage())
    trial = SimpleNamespace(
        id="trial-rich",
        name="trial-rich",
        model="xai/redacted-model",
        trial_s3_key=prefix,
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert [s["message"] for s in trajectory["steps"]] == ["a", "b"]
