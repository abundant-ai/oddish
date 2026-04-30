"""Tests for api.services.summarize_trajectory."""

from __future__ import annotations

from copy import deepcopy

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
    assert any(p["text"] == "[image omitted] (x1)" for p in msg_parts)
    obs_parts = out["steps"][0]["observation"]["results"][0]["content"]
    assert obs_parts[0]["type"] == "text"
    assert obs_parts[0]["text"] == "[image omitted] (x1)"


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


import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _trajectory_with_steps(step_ids: list[int]) -> dict:
    return {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [_make_step(sid) for sid in step_ids],
        "notes": None,
        "final_metrics": None,
    }


def _fake_client_returning(text: str) -> MagicMock:
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=text)]
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)
    return fake_client


@pytest.mark.asyncio
async def test_generate_returns_persistable_summary():
    from api.services.summarize_trajectory import (
        MODEL,
        SCHEMA_VERSION,
        generate,
    )

    payload = json.dumps(
        {
            "summary": "Agent reproduced and fixed a flaky test.",
            "highlights": [
                {"step_id": 1, "title": "Reproduces failure", "why": "First confirmation."},
                {"step_id": 3, "title": "Lands the fix", "why": "Patch applied."},
            ],
        }
    )
    fake = _fake_client_returning(payload)
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await generate(_trajectory_with_steps([1, 2, 3]))

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["model"] == MODEL
    assert "generated_at" in result
    assert result["summary"].startswith("Agent reproduced")
    assert [h["step_id"] for h in result["highlights"]] == [1, 3]


@pytest.mark.asyncio
async def test_generate_drops_highlights_with_unknown_step_ids():
    from api.services.summarize_trajectory import generate

    payload = json.dumps(
        {
            "summary": "x",
            "highlights": [
                {"step_id": 1, "title": "ok", "why": "ok"},
                {"step_id": 999, "title": "bogus", "why": "model hallucinated"},
            ],
        }
    )
    fake = _fake_client_returning(payload)
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await generate(_trajectory_with_steps([1, 2, 3]))

    assert [h["step_id"] for h in result["highlights"]] == [1]


@pytest.mark.asyncio
async def test_generate_strips_code_fences_around_json():
    from api.services.summarize_trajectory import generate

    body = json.dumps({"summary": "ok", "highlights": []})
    fenced = f"```json\n{body}\n```"
    fake = _fake_client_returning(fenced)
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await generate(_trajectory_with_steps([1]))
    assert result["summary"] == "ok"
    assert result["highlights"] == []


@pytest.mark.asyncio
async def test_generate_raises_on_malformed_json():
    from api.services.summarize_trajectory import (
        SummaryGenerationError,
        generate,
    )

    fake = _fake_client_returning("not json at all")
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        with pytest.raises(SummaryGenerationError):
            await generate(_trajectory_with_steps([1]))
