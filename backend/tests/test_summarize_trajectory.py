"""Tests for api.services.summarize_trajectory."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.summarize_trajectory import (
    MAX_TEXT_CHARS,
    SCHEMA_VERSION,
    TaskContext,
    TRUNCATE_HEAD,
    TRUNCATE_TAIL,
    build_task_context,
    get_or_generate_summary,
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


# ---------------------------------------------------------------------------
# preprocess
# ---------------------------------------------------------------------------


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
    out = preprocess({"steps": [step]})
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
    out = preprocess({"steps": [step]})
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
    out = preprocess({"steps": [step]})
    args = out["steps"][0]["tool_calls"][0]["arguments"]
    assert args["path"] == "main.py"
    assert "[...truncated" in args["content"]
    assert len(args["content"]) < len(huge)


def test_preprocess_truncates_observation_string_content():
    huge = "L" * (MAX_TEXT_CHARS + 1000)
    step = _make_step(1, observation={"results": [{"source_call_id": "c1", "content": huge}]})
    out = preprocess({"steps": [step]})
    content = out["steps"][0]["observation"]["results"][0]["content"]
    assert "[...truncated" in content
    assert len(content) < len(huge)


def test_preprocess_does_not_mutate_input():
    huge = "Q" * (MAX_TEXT_CHARS + 100)
    step = _make_step(1, reasoning_content=huge)
    trajectory = {"steps": [step]}
    snapshot = deepcopy(trajectory)
    preprocess(trajectory)
    assert trajectory == snapshot


# ---------------------------------------------------------------------------
# generate (via AnalyzerBlock + injected fake client)
# ---------------------------------------------------------------------------


def _trajectory_with_steps(step_ids: list[int]) -> dict:
    return {"steps": [_make_step(sid) for sid in step_ids]}


def _minimal_ctx() -> TaskContext:
    return TaskContext(
        task_name="test_task", instruction=None, final_reward=None,
        model_used=None, verifier_output=None,
    )


def _fake_llm(payload: str):
    from api.services.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    return FakeAnalyzerLLMClient(chunks=[payload])


def _patch_block_persistence(monkeypatch):
    from api.services.blocks.analyzer.analyzer_block import AnalyzerBlock
    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", AsyncMock())
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", AsyncMock())


@pytest.mark.asyncio
async def test_generate_returns_persistable_summary(monkeypatch):
    from api.services.summarize_trajectory import generate
    _patch_block_persistence(monkeypatch)
    payload = json.dumps({
        "summary": "Agent reproduced and fixed a flaky test.",
        "highlights": [{"step_id": 1, "title": "Repro", "why": "First."},
                       {"step_id": 3, "title": "Fix", "why": "Patch."}],
        "components": [{"step_ids": [1, 2], "trajectory_component": "debugging", "summary": "d"}],
    })
    result = await generate(
        _trajectory_with_steps([1, 2, 3]), _minimal_ctx(), client=_fake_llm(payload),
    )
    assert result["schema_version"] == SCHEMA_VERSION == "4"
    assert result["summary"].startswith("Agent reproduced")
    assert [h["step_id"] for h in result["highlights"]] == [1, 3]
    assert result["components"][0]["trajectory_component"] == "debugging"
    assert "phases" not in result


@pytest.mark.asyncio
async def test_generate_drops_highlights_with_unknown_step_ids(monkeypatch):
    from api.services.summarize_trajectory import generate
    _patch_block_persistence(monkeypatch)
    payload = json.dumps({
        "summary": "x",
        "highlights": [{"step_id": 1, "title": "ok", "why": "ok"},
                       {"step_id": 999, "title": "bogus", "why": "hallucinated"}],
        "components": [],
    })
    result = await generate(
        _trajectory_with_steps([1, 2, 3]), _minimal_ctx(), client=_fake_llm(payload),
    )
    assert [h["step_id"] for h in result["highlights"]] == [1]


@pytest.mark.asyncio
async def test_generate_strips_code_fences_around_json(monkeypatch):
    from api.services.summarize_trajectory import generate
    _patch_block_persistence(monkeypatch)
    body = json.dumps({"summary": "ok", "highlights": [], "components": []})
    result = await generate(
        _trajectory_with_steps([1]), _minimal_ctx(), client=_fake_llm(f"```json\n{body}\n```"),
    )
    assert result["summary"] == "ok"


@pytest.mark.asyncio
async def test_generate_raises_on_malformed_json(monkeypatch):
    from api.services.summarize_trajectory import SummaryGenerationError, generate
    _patch_block_persistence(monkeypatch)
    with pytest.raises(SummaryGenerationError):
        await generate(_trajectory_with_steps([1]), _minimal_ctx(), client=_fake_llm("not json"))


@pytest.mark.asyncio
async def test_generate_raises_when_model_returns_non_object_json(monkeypatch):
    from api.services.summarize_trajectory import SummaryGenerationError, generate
    _patch_block_persistence(monkeypatch)
    with pytest.raises(SummaryGenerationError):
        await generate(_trajectory_with_steps([1]), _minimal_ctx(), client=_fake_llm("[1,2,3]"))


@pytest.mark.asyncio
async def test_generate_wraps_client_errors(monkeypatch):
    from api.services.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summarize_trajectory import SummaryGenerationError, generate
    _patch_block_persistence(monkeypatch)
    client = FakeAnalyzerLLMClient(chunks=[], exc=RuntimeError("boom"))
    with pytest.raises(SummaryGenerationError):
        await generate(_trajectory_with_steps([1]), _minimal_ctx(), client=client)


@pytest.mark.asyncio
async def test_generate_returns_components(monkeypatch):
    from api.services.summarize_trajectory import generate
    _patch_block_persistence(monkeypatch)
    payload = json.dumps({
        "summary": "s", "highlights": [],
        "components": [
            {"step_ids": [1, 2], "trajectory_component": "reading_files", "summary": "look"},
            {"step_ids": [3], "trajectory_component": "implementing", "summary": "code"},
        ],
    })
    result = await generate(
        _trajectory_with_steps([1, 2, 3]), _minimal_ctx(), client=_fake_llm(payload),
    )
    assert [c["trajectory_component"] for c in result["components"]] == ["reading_files", "implementing"]


# ---------------------------------------------------------------------------
# get_or_generate_summary
# ---------------------------------------------------------------------------


def _fake_trial(*, has_trajectory: bool):
    return SimpleNamespace(
        id="t-1",
        name="trial-0",
        trial_s3_key="trials/t-1/",
        has_trajectory=has_trajectory,
        agent="claude-code",
        finished_at=None,
    )


def _fake_session():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_get_or_generate_returns_block_when_fresh():
    cached = {"schema_version": "4", "summary": "cached", "highlights": [], "components": []}
    trial = _fake_trial(has_trajectory=True)
    session = _fake_session()
    with patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new_callable=AsyncMock, return_value=cached,
    ), patch(
        "api.services.summarize_trajectory.generate", new_callable=AsyncMock,
    ) as gen:
        result = await get_or_generate_summary(session, trial)
    assert result == cached
    gen.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_generate_returns_none_when_no_trajectory():
    trial = _fake_trial(has_trajectory=False)
    session = _fake_session()
    with patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new_callable=AsyncMock, return_value=None,
    ):
        result = await get_or_generate_summary(session, trial)
    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_generate_persists_on_miss():
    trial = _fake_trial(has_trajectory=True)
    session = _fake_session()
    fresh = {"schema_version": "4", "summary": "fresh", "highlights": [], "components": []}

    async def fake_traj(_t):
        return {"steps": [{"step_id": 1}]}

    async def fake_ctx(_t):
        return _minimal_ctx()

    with patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new_callable=AsyncMock, return_value=None,
    ), patch(
        "api.services.summarize_trajectory.read_trial_trajectory", new=fake_traj,
    ), patch(
        "api.services.summarize_trajectory.build_task_context", new=fake_ctx,
    ), patch(
        "api.services.summarize_trajectory.generate",
        new_callable=AsyncMock, return_value=fresh,
    ):
        result = await get_or_generate_summary(session, trial)
    assert result == fresh
    session.execute.assert_awaited_once()   # the mirror UPDATE
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_generate_fetches_trajectory_and_context_in_parallel():
    import asyncio as _asyncio

    trial = _fake_trial(has_trajectory=True)
    session = _fake_session()
    started: list[str] = []
    finished: list[str] = []

    async def slow_trajectory(_t):
        started.append("trajectory")
        await _asyncio.sleep(0.05)
        finished.append("trajectory")
        return {"steps": [{"step_id": 1}]}

    async def slow_context(_t):
        started.append("context")
        await _asyncio.sleep(0.05)
        finished.append("context")
        return _minimal_ctx()

    fresh = {"schema_version": "4", "summary": "ok", "highlights": [], "components": []}

    with patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new_callable=AsyncMock, return_value=None,
    ), patch(
        "api.services.summarize_trajectory.read_trial_trajectory", new=slow_trajectory,
    ), patch(
        "api.services.summarize_trajectory.build_task_context", new=slow_context,
    ), patch(
        "api.services.summarize_trajectory.generate",
        new_callable=AsyncMock, return_value=fresh,
    ):
        await get_or_generate_summary(session, trial)

    assert {started[0], started[1]} == {"trajectory", "context"}
    assert len(finished) == 2


# ---------------------------------------------------------------------------
# build_task_context
# ---------------------------------------------------------------------------


def _trial_with_task(*, task_name, reward, model, harbor_config=None):
    return SimpleNamespace(
        id="t-1", name="trial-0", trial_s3_key="trials/t-1/",
        reward=reward, model=model, harbor_config=harbor_config,
        task=SimpleNamespace(name=task_name),
    )


@pytest.mark.asyncio
async def test_build_task_context_pulls_all_fields():
    trial = _trial_with_task(task_name="solve_x", reward=0.75, model="claude-sonnet-4-6")

    async def fake_instruction(_t):
        return "Solve the puzzle."

    async def fake_verifier(_t):
        return "PASS\n"

    with patch(
        "api.services.summarize_trajectory.read_trial_instruction", new=fake_instruction,
    ), patch(
        "api.services.summarize_trajectory.read_trial_verifier_output", new=fake_verifier,
    ):
        ctx = await build_task_context(trial)

    assert ctx == TaskContext(
        task_name="solve_x", instruction="Solve the puzzle.", final_reward=0.75,
        model_used="claude-sonnet-4-6", verifier_output="PASS\n",
    )


@pytest.mark.asyncio
async def test_build_task_context_handles_missing_fields():
    trial = _trial_with_task(task_name="solve_x", reward=None, model=None)

    async def fake_none(_t):
        return None

    with patch(
        "api.services.summarize_trajectory.read_trial_instruction", new=fake_none,
    ), patch(
        "api.services.summarize_trajectory.read_trial_verifier_output", new=fake_none,
    ):
        ctx = await build_task_context(trial)

    assert ctx.task_name == "solve_x"
    assert ctx.instruction is None
    assert ctx.final_reward is None
    assert ctx.model_used is None
    assert ctx.verifier_output is None


@pytest.mark.asyncio
async def test_build_task_context_falls_back_to_harbor_config_model():
    trial = _trial_with_task(
        task_name="solve_x", reward=None, model=None,
        harbor_config={"agent": {"model": "claude-sonnet-4-6"}},
    )

    async def fake_none(_t):
        return None

    with patch(
        "api.services.summarize_trajectory.read_trial_instruction", new=fake_none,
    ), patch(
        "api.services.summarize_trajectory.read_trial_verifier_output", new=fake_none,
    ):
        ctx = await build_task_context(trial)

    assert ctx.model_used == "claude-sonnet-4-6"


def test_schema_version_is_four():
    assert SCHEMA_VERSION == "4"
