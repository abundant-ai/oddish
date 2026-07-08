"""Grok Build session-store -> ATIF trajectory conversion.

The grok CLI's headless stdout is text-only; the real trajectory (tool calls +
token usage) is persisted under ``$GROK_HOME/sessions/.../<id>/``. These tests
cover turning that captured store into a rich trajectory.
"""

from __future__ import annotations

import json
from pathlib import Path

from oddish.workers.agents.grok_build_session import (
    build_session_trajectory,
    extract_token_totals,
    find_session_dir,
)


def _write_session(root: Path, session_id: str) -> Path:
    """Create a captured ``grok-session`` tree with a nested session dir."""
    session_dir = root / "sessions" / "%2Fapp" / session_id
    session_dir.mkdir(parents=True)
    updates = [
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Let me read the file."},
                }
            },
        },
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-1",
                    "title": "read_file",
                    "rawInput": {"path": "/app/main.py"},
                }
            },
        },
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "status": "completed",
                    "content": [{"type": "text", "text": "print('hi')"}],
                }
            },
        },
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Done."},
                }
            },
        },
    ]
    (session_dir / "updates.jsonl").write_text(
        "\n".join(json.dumps(u) for u in updates), encoding="utf-8"
    )
    events = [
        {"type": "turn_started", "turn_number": 0},
        {
            "type": "turn_ended",
            "outcome": "success",
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 340,
                "cached_tokens": 800,
            },
        },
    ]
    (session_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )
    return session_dir


def test_find_session_dir_prefers_exact_id(tmp_path):
    capture = tmp_path / "grok-session"
    _write_session(capture, "sess-abc")
    found = find_session_dir(capture, "sess-abc")
    assert found is not None and found.name == "sess-abc"


def test_find_session_dir_falls_back_to_jsonl_dir(tmp_path):
    capture = tmp_path / "grok-session"
    _write_session(capture, "sess-xyz")
    found = find_session_dir(capture, None)
    assert found is not None and found.name == "sess-xyz"


def test_extract_token_totals(tmp_path):
    capture = tmp_path / "grok-session"
    session_dir = _write_session(capture, "sess-tok")
    totals = extract_token_totals(session_dir)
    assert totals == {"prompt": 1200, "completion": 340, "cache": 800}


def test_build_trajectory_has_tool_calls_and_tokens(tmp_path):
    capture = tmp_path / "grok-session"
    _write_session(capture, "sess-1")
    traj = build_session_trajectory(
        capture, session_id="sess-1", agent_version="0.2.91", model_name="xai/v9"
    )
    assert traj is not None
    # More than the degenerate single text step the stdout path produces.
    assert len(traj.steps) >= 3
    tool_steps = [s for s in traj.steps if s.tool_calls]
    assert tool_steps, "expected at least one tool_call step"
    assert tool_steps[0].tool_calls[0].function_name == "read_file"
    assert tool_steps[0].tool_calls[0].arguments == {"path": "/app/main.py"}
    # The tool result is attached as an observation.
    assert tool_steps[0].observation is not None
    fm = traj.final_metrics
    assert fm.total_prompt_tokens == 1200
    assert fm.total_completion_tokens == 340
    assert fm.total_cached_tokens == 800
    assert fm.total_steps == len(traj.steps)


def test_missing_capture_returns_none(tmp_path):
    assert (
        build_session_trajectory(
            tmp_path / "does-not-exist",
            session_id=None,
            agent_version="x",
            model_name=None,
        )
        is None
    )


def test_empty_session_returns_none(tmp_path):
    capture = tmp_path / "grok-session"
    (capture / "sessions").mkdir(parents=True)
    assert (
        build_session_trajectory(
            capture, session_id=None, agent_version="x", model_name=None
        )
        is None
    )
