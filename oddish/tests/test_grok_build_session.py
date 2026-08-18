"""Grok Build session-store -> ATIF trajectory conversion.

The grok CLI's headless stdout is text-only; the real trajectory (tool calls +
token usage) is persisted under ``$GROK_HOME/sessions/.../<id>/``. These tests
cover turning that captured store into a rich trajectory.
"""

from __future__ import annotations

import json
from pathlib import Path

from oddish.workers.agents.grok_build_session import (
    OBSERVATION_CLIP_CHARS,
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
                    # Grok nests tool results two levels deep -- a flat
                    # ``{"type": "text"}`` item here is not a shape it emits.
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": "print('hi')"},
                        }
                    ],
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


def test_find_session_dir_known_id_uses_lone_session(tmp_path):
    # Known session id, no exact match, but a single session -> safe to use it.
    capture = tmp_path / "grok-session"
    _write_session(capture, "sess-actual")
    found = find_session_dir(capture, "sess-from-stdout")
    assert found is not None and found.name == "sess-actual"


def test_find_session_dir_known_id_refuses_ambiguous(tmp_path):
    # Known session id, no exact match, multiple sessions -> refuse to guess.
    capture = tmp_path / "grok-session"
    _write_session(capture, "sess-a")
    _write_session(capture, "sess-b")
    assert find_session_dir(capture, "sess-missing") is None
    # ...but an exact match still wins even with multiple sessions present.
    assert find_session_dir(capture, "sess-b").name == "sess-b"


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


def test_tokens_fall_back_to_sampling_log(tmp_path):
    # Session stream has no usage; grok's logs/unified.jsonl (sampling log) does.
    capture = tmp_path / "grok-session"
    session_dir = capture / "sessions" / "%2Fapp" / "sess-log"
    session_dir.mkdir(parents=True)
    (session_dir / "updates.jsonl").write_text(
        json.dumps(
            {
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "c1",
                        "title": "run_terminal_command",
                        "rawInput": {"command": "ls"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    logs = capture / "logs"
    logs.mkdir(parents=True)
    (logs / "unified.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"msg": "sampling_log", "prompt_tokens": 500}),
                json.dumps({"msg": "sampling_log", "completion_tokens": 120}),
                json.dumps({"msg": "sampling_log", "prompt_tokens": 700}),
            ]
        ),
        encoding="utf-8",
    )
    traj = build_session_trajectory(
        capture, session_id="sess-log", agent_version="x", model_name="xai/v9"
    )
    assert traj is not None
    assert traj.final_metrics.total_prompt_tokens == 1200
    assert traj.final_metrics.total_completion_tokens == 120


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


# ---------------------------------------------------------------------------
# Tool results (observations)
#
# Every payload below is copied from a real captured ``updates.jsonl``
# (trial ``bitcoinj-script-verify-gate-1309f53a-221``). Grok puts a short
# summary in ``content`` -- frequently empty or absent -- and the environment's
# actual response in ``rawOutput``, keyed by tool type.
# ---------------------------------------------------------------------------


def _observation_for(tmp_path, update: dict) -> str | None:
    """Run one ``tool_call`` + ``tool_call_update`` pair through the converter."""
    session_dir = tmp_path / "grok-session" / "sessions" / "%2Fapp" / "s"
    session_dir.mkdir(parents=True)
    records = [
        {
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "tool",
                    "rawInput": {},
                }
            }
        },
        {"params": {"update": {"sessionUpdate": "tool_call_update", **update}}},
    ]
    (session_dir / "updates.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8"
    )
    traj = build_session_trajectory(
        tmp_path / "grok-session", session_id="s", agent_version="x", model_name="m"
    )
    assert traj is not None
    step = traj.steps[0]
    if step.observation is None:
        return None
    return step.observation.results[0].content


def test_nested_content_becomes_observation(tmp_path):
    text = _observation_for(
        tmp_path,
        {
            "toolCallId": "c1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "found 43"}}
            ],
        },
    )
    assert text is not None, "nested tool result was dropped"
    assert "found 43" in text


def test_list_dir_result_comes_from_raw_output(tmp_path):
    # The customer-visible case: ``content`` is null, so the directory listing
    # only exists in ``rawOutput``.
    text = _observation_for(
        tmp_path,
        {
            "toolCallId": "c1",
            "status": "completed",
            "content": None,
            "rawOutput": {
                "type": "ListDir",
                "Content": {"content": "- /app/src/\n  - AUTHORS\n  - base/\n"},
            },
        },
    )
    assert text is not None, "list_dir result was dropped"
    assert "AUTHORS" in text


def test_bash_result_records_command_cwd_and_exit_code(tmp_path):
    text = _observation_for(
        tmp_path,
        {
            "toolCallId": "c1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": ""}}],
            "rawOutput": {
                "type": "Bash",
                "command": "gradle :core:test",
                "current_dir": "/app/src",
                "exit_code": 1,
                "output_for_prompt": "BUILD FAILED",
                "timed_out": False,
                "truncated": False,
            },
        },
    )
    assert text is not None
    assert "gradle :core:test" in text
    assert "/app/src" in text
    assert "1" in text
    assert "BUILD FAILED" in text


def test_grep_stdout_byte_array_is_decoded(tmp_path):
    stdout = list(b"Found at least 43 matching lines\n/app/src/Foo.java")
    text = _observation_for(
        tmp_path,
        {
            "toolCallId": "c1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "found 43"}}
            ],
            "rawOutput": {"type": "GrepSearch", "stdout": stdout, "exit_code": 0},
        },
    )
    assert text is not None
    assert "Found at least 43 matching lines" in text
    assert "/app/src/Foo.java" in text


def test_diff_content_records_path_and_both_sides(tmp_path):
    text = _observation_for(
        tmp_path,
        {
            "toolCallId": "c1",
            "status": "completed",
            "content": [
                {
                    "type": "diff",
                    "path": "/app/src/ScriptExecution.java",
                    "oldText": "boolean validSig = pubkey.verify(x);",
                    "newText": "if (!ScriptPattern.isP2PKH(s)) throw new E();",
                }
            ],
        },
    )
    assert text is not None, "file edit was dropped"
    assert "/app/src/ScriptExecution.java" in text
    assert "boolean validSig" in text
    assert "isP2PKH" in text


def test_task_output_result_is_used_and_ansi_stripped(tmp_path):
    text = _observation_for(
        tmp_path,
        {
            "toolCallId": "c1",
            "status": "completed",
            "content": None,
            "rawOutput": {
                "type": "TaskOutput",
                "Result": {
                    "command": "gradle :core:test",
                    "exit_code": 0,
                    "output": "\x1b[1m\x1b[0;1mBUILD SUCCESSFUL\x1b[m\x1b[38D",
                },
            },
        },
    )
    assert text is not None
    assert "BUILD SUCCESSFUL" in text
    assert "\x1b" not in text, "ANSI escapes leaked into the observation"


def test_observation_is_clipped(tmp_path):
    text = _observation_for(
        tmp_path,
        {
            "toolCallId": "c1",
            "status": "completed",
            "content": None,
            "rawOutput": {
                "type": "ReadFile",
                "FileContent": {"content": "x" * (OBSERVATION_CLIP_CHARS * 3)},
            },
        },
    )
    assert text is not None
    assert len(text) < OBSERVATION_CLIP_CHARS * 2
    assert "truncated" in text.lower()


def test_result_with_no_usable_payload_leaves_no_observation(tmp_path):
    assert (
        _observation_for(
            tmp_path,
            {"toolCallId": "c1", "status": "completed", "content": None},
        )
        is None
    )
