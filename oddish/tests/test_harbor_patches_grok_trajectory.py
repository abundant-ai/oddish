"""Grok session capture + trajectory rebuild for ephemeral-Harbor runs.

Harbor's stock grok agents run grok headless with non-streaming JSON output,
which collapses a multi-turn run into a single-step trajectory and never
persists the CLI session store. The oddish harbor patches capture
``$GROK_HOME/sessions`` into the trial logs around the agent run and rebuild
``trajectory.json`` from the capture when it is richer.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

harbor_patches = importlib.import_module("oddish.workers.harbor.patches")

_SESSION_ID = "019f0000-aaaa-bbbb-cccc-ddddeeeeffff"


def _collapsed_result_payload() -> dict:
    return {
        "text": "Final answer only.",
        "stopReason": "EndTurn",
        "sessionId": _SESSION_ID,
        "num_turns": 3,
        "modelUsage": {"m": {"inputTokens": 10, "outputTokens": 5}},
    }


def _write_collapsed_trajectory(logs_dir: Path) -> None:
    (logs_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": _SESSION_ID,
                "agent": {
                    "name": "grok-build-api-key-no-search",
                    "version": "grok 0.2.106 (bde89716f6)",
                    "model_name": "v9m-rl-learnability-tp8",
                },
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "message": "Final answer only.",
                    }
                ],
            }
        )
    )


def _write_session_store(logs_dir: Path) -> None:
    session_dir = logs_dir / "grok-session" / "sessions" / "%2Fapp" / _SESSION_ID
    session_dir.mkdir(parents=True)
    updates = [
        {
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Reading the input."},
                }
            }
        },
        {
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-1",
                    "title": "run_terminal_command",
                    "rawInput": {"command": "ls /app"},
                }
            }
        },
        {
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "status": "completed",
                    "content": [{"type": "text", "text": "main.py\n"}],
                }
            }
        },
        {
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Final answer only."},
                }
            }
        },
    ]
    (session_dir / "updates.jsonl").write_text(
        "\n".join(json.dumps(u) for u in updates)
    )


def _fake_agent(logs_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(logs_dir=logs_dir, model_name="v9m-rl-learnability-tp8")


def test_rebuild_replaces_collapsed_trajectory_with_session_steps(tmp_path):
    (tmp_path / "grok-build.json").write_text(json.dumps(_collapsed_result_payload()))
    _write_collapsed_trajectory(tmp_path)
    _write_session_store(tmp_path)

    harbor_patches._rebuild_grok_trajectory_from_session(_fake_agent(tmp_path))

    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    steps = trajectory["steps"]
    assert len(steps) == 3
    tool_steps = [s for s in steps if s.get("tool_calls")]
    assert tool_steps[0]["tool_calls"][0]["function_name"] == "run_terminal_command"
    assert tool_steps[0]["tool_calls"][0]["arguments"] == {"command": "ls /app"}
    assert tool_steps[0]["observation"]["results"][0]["content"] == "main.py\n"
    # Version/model carry over from what the stock agent resolved.
    assert trajectory["agent"]["version"] == "grok 0.2.106 (bde89716f6)"
    assert trajectory["agent"]["model_name"] == "v9m-rl-learnability-tp8"


def test_rebuild_keeps_existing_trajectory_when_session_is_not_richer(tmp_path):
    (tmp_path / "grok-build.json").write_text(json.dumps(_collapsed_result_payload()))
    _write_collapsed_trajectory(tmp_path)
    before = (tmp_path / "trajectory.json").read_text()

    harbor_patches._rebuild_grok_trajectory_from_session(_fake_agent(tmp_path))

    assert (tmp_path / "trajectory.json").read_text() == before


def test_populate_post_run_wrapper_runs_original_then_rebuilds(tmp_path):
    calls: list[str] = []

    def orig(self, context):
        calls.append("orig")
        _write_collapsed_trajectory(tmp_path)

    (tmp_path / "grok-build.json").write_text(json.dumps(_collapsed_result_payload()))
    _write_session_store(tmp_path)

    wrapped = harbor_patches._wrap_grok_populate_post_run(orig)
    wrapped(_fake_agent(tmp_path), context=None)

    assert calls == ["orig"]
    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert len(trajectory["steps"]) == 3


def test_run_wrapper_captures_session_even_when_run_fails(tmp_path):
    commands: list[str] = []

    class _Agent:
        model_name = "m"

        async def exec_as_agent(self, environment, *, command):
            commands.append(command)

    async def orig(self, environment):
        raise RuntimeError("agent died")

    wrapped = harbor_patches._wrap_grok_run(orig)
    agent = _Agent()

    async def _invoke():
        try:
            await wrapped(agent, "env")
        except RuntimeError:
            return True
        return False

    assert asyncio.run(_invoke())
    assert commands[0] == harbor_patches._GROK_CLEAR_SESSIONS_CMD
    assert commands[-1] == harbor_patches._GROK_CAPTURE_SESSIONS_CMD


def test_grok_session_id_parses_single_dict_and_ndjson(tmp_path):
    (tmp_path / "grok-build.json").write_text(json.dumps(_collapsed_result_payload()))
    assert harbor_patches._grok_session_id(tmp_path) == _SESSION_ID

    (tmp_path / "grok-build.json").write_text(
        "\n".join(
            [
                json.dumps({"type": "text", "data": "hi"}),
                json.dumps({"type": "end", "sessionId": "ndjson-session"}),
            ]
        )
    )
    assert harbor_patches._grok_session_id(tmp_path) == "ndjson-session"
