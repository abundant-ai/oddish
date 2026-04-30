"""Tests for api.services.summarize_trajectory."""

from __future__ import annotations

from copy import deepcopy

import pytest

from api.services.summarize_trajectory import (
    MAX_TEXT_CHARS,
    TRUNCATE_HEAD,
    TRUNCATE_TAIL,
    preprocess,
)


def _make_step(step_id: int, **overrides) -> dict:
    base: dict = {
        "step_id": step_id,
        "timestamp": "2026-04-30T12:00:00Z",
        "source": "agent",
        "model_name": "claude-sonnet-4-6",
        "message": "hello",
        "reasoning_content": None,
        "tool_calls": None,
        "observation": None,
        "metrics": None,
    }
    base.update(overrides)
    return base


def test_preprocess_leaves_small_fields_untouched():
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "claude-code", "version": "1", "model_name": "x"},
        "steps": [_make_step(1, message="short")],
        "notes": None,
        "final_metrics": None,
    }
    expected = deepcopy(trajectory)
    assert preprocess(trajectory) == expected


def test_preprocess_truncates_large_reasoning_content():
    long_text = "A" * 800 + "B" * 1500 + "C" * 500  # 2800 chars
    step = _make_step(1, reasoning_content=long_text)
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    rc = out["steps"][0]["reasoning_content"]
    assert rc.startswith("A" * TRUNCATE_HEAD)
    assert rc.endswith("C" * TRUNCATE_TAIL)
    assert "[...truncated" in rc
    assert len(rc) < len(long_text)


def test_preprocess_strips_image_content_parts():
    step = _make_step(
        1,
        message=[
            {"type": "text", "text": "look at this:"},
            {"type": "image", "source": {"media_type": "image/png", "path": "x.png"}},
            {"type": "text", "text": "thoughts?"},
        ],
        observation={
            "results": [
                {
                    "source_call_id": "c1",
                    "content": [
                        {"type": "image", "source": {"media_type": "image/png", "path": "y.png"}},
                    ],
                }
            ]
        },
    )
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    msg_parts = out["steps"][0]["message"]
    assert {p["type"] for p in msg_parts} == {"text"}
    assert any("[image omitted]" in p["text"] for p in msg_parts)
    obs_parts = out["steps"][0]["observation"]["results"][0]["content"]
    assert obs_parts[0]["type"] == "text"
    assert "[image omitted]" in obs_parts[0]["text"]


def test_preprocess_truncates_tool_call_argument_values():
    huge = "Z" * (MAX_TEXT_CHARS + 500)
    step = _make_step(
        1,
        tool_calls=[
            {
                "tool_call_id": "t1",
                "function_name": "edit_file",
                "arguments": {"path": "main.py", "content": huge},
            }
        ],
    )
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    args = out["steps"][0]["tool_calls"][0]["arguments"]
    assert args["path"] == "main.py"  # small string untouched
    assert "[...truncated" in args["content"]
    assert len(args["content"]) < len(huge)


def test_preprocess_truncates_observation_string_content():
    huge = "L" * (MAX_TEXT_CHARS + 1000)
    step = _make_step(
        1,
        observation={
            "results": [{"source_call_id": "c1", "content": huge}]
        },
    )
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    content = out["steps"][0]["observation"]["results"][0]["content"]
    assert "[...truncated" in content
    assert len(content) < len(huge)


def test_preprocess_does_not_mutate_input():
    huge = "Q" * (MAX_TEXT_CHARS + 100)
    step = _make_step(1, reasoning_content=huge)
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    snapshot = deepcopy(trajectory)
    preprocess(trajectory)
    assert trajectory == snapshot
