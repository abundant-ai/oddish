"""Tests for api.services.summarize_trajectory."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

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


def _trajectory_with_steps(step_ids: list[int]) -> dict:
    return {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [_make_step(sid) for sid in step_ids],
        "notes": None,
        "final_metrics": None,
    }


def _minimal_ctx():
    """Return a TaskContext with all optional fields None."""
    from api.services.summarize_trajectory import TaskContext

    return TaskContext(
        task_name="test_task",
        instruction=None,
        final_reward=None,
        model_used=None,
        verifier_output=None,
    )


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
        result = await generate(_trajectory_with_steps([1, 2, 3]), _minimal_ctx())

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
        result = await generate(_trajectory_with_steps([1, 2, 3]), _minimal_ctx())

    assert [h["step_id"] for h in result["highlights"]] == [1]


@pytest.mark.asyncio
async def test_generate_strips_code_fences_around_json():
    from api.services.summarize_trajectory import generate

    body = json.dumps({"summary": "ok", "highlights": []})
    fenced = f"```json\n{body}\n```"
    fake = _fake_client_returning(fenced)
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await generate(_trajectory_with_steps([1]), _minimal_ctx())
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
            await generate(_trajectory_with_steps([1]), _minimal_ctx())


@pytest.mark.asyncio
async def test_generate_raises_when_model_returns_non_object_json():
    from api.services.summarize_trajectory import (
        SummaryGenerationError,
        generate,
    )

    fake = _fake_client_returning("[1, 2, 3]")
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        with pytest.raises(SummaryGenerationError):
            await generate(_trajectory_with_steps([1]), _minimal_ctx())


@pytest.mark.asyncio
async def test_generate_wraps_anthropic_errors_in_summary_generation_error():
    """A non-JSON failure (e.g. AuthenticationError, network error) should
    surface as SummaryGenerationError so the endpoint can return 502."""
    from api.services.summarize_trajectory import (
        SummaryGenerationError,
        generate,
    )

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        side_effect=RuntimeError("anthropic auth failed")
    )
    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        with pytest.raises(SummaryGenerationError):
            await generate(_trajectory_with_steps([1]), _minimal_ctx())


# ---------------------------------------------------------------------------
# get_or_generate_summary tests
# ---------------------------------------------------------------------------

from types import SimpleNamespace


def _fake_trial(*, has_trajectory: bool, trajectory_summary: dict | None):
    return SimpleNamespace(
        id="t-1",
        name="trial-0",
        trial_s3_key="trials/t-1/",
        has_trajectory=has_trajectory,
        trajectory_summary=trajectory_summary,
    )


def _fake_session():
    """Async session stub: refresh/execute/commit are no-ops for unit tests."""
    session = MagicMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_get_or_generate_returns_db_column_when_fresh():
    from api.services.summarize_trajectory import (
        SCHEMA_VERSION,
        get_or_generate_summary,
    )

    cached = {
        "schema_version": SCHEMA_VERSION,
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-05-02T00:00:00Z",
        "summary": "cached",
        "highlights": [],
    }
    trial = _fake_trial(has_trajectory=True, trajectory_summary=cached)
    session = _fake_session()

    with patch(
        "api.services.summarize_trajectory.generate", new_callable=AsyncMock
    ) as fake_generate:
        result = await get_or_generate_summary(session, trial)

    assert result == cached
    fake_generate.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_generate_returns_none_when_no_trajectory():
    from api.services.summarize_trajectory import get_or_generate_summary

    trial = _fake_trial(has_trajectory=False, trajectory_summary=None)
    session = _fake_session()

    result = await get_or_generate_summary(session, trial)

    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_generate_persists_on_miss():
    from api.services.summarize_trajectory import (
        SCHEMA_VERSION,
        get_or_generate_summary,
    )

    trial = _fake_trial(has_trajectory=True, trajectory_summary=None)
    session = _fake_session()

    fresh = {
        "schema_version": SCHEMA_VERSION,
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-05-02T00:00:00Z",
        "summary": "fresh",
        "highlights": [],
    }

    async def fake_read_trajectory(trial_arg):
        return {"steps": [{"step_id": 1}]}

    async def fake_build_task_context(trial_arg):
        return TaskContext(
            task_name="t",
            instruction=None,
            final_reward=None,
            model_used=None,
            verifier_output=None,
        )

    with patch(
        "api.services.summarize_trajectory.read_trial_trajectory",
        new=fake_read_trajectory,
    ), patch(
        "api.services.summarize_trajectory.build_task_context",
        new=fake_build_task_context,
    ), patch(
        "api.services.summarize_trajectory.generate",
        new_callable=AsyncMock,
        return_value=fresh,
    ):
        result = await get_or_generate_summary(session, trial)

    assert result == fresh
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_generate_regenerates_on_stale_schema():
    from api.services.summarize_trajectory import (
        SCHEMA_VERSION,
        get_or_generate_summary,
    )

    stale = {
        "schema_version": "0",
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-04-30T00:00:00Z",
        "summary": "stale",
        "highlights": [],
    }
    trial = _fake_trial(has_trajectory=True, trajectory_summary=stale)
    session = _fake_session()

    fresh = {**stale, "schema_version": SCHEMA_VERSION, "summary": "fresh"}

    async def fake_read_trajectory(trial_arg):
        return {"steps": [{"step_id": 1}]}

    async def fake_build_task_context(trial_arg):
        return TaskContext(
            task_name="t",
            instruction=None,
            final_reward=None,
            model_used=None,
            verifier_output=None,
        )

    with patch(
        "api.services.summarize_trajectory.read_trial_trajectory",
        new=fake_read_trajectory,
    ), patch(
        "api.services.summarize_trajectory.build_task_context",
        new=fake_build_task_context,
    ), patch(
        "api.services.summarize_trajectory.generate",
        new_callable=AsyncMock,
        return_value=fresh,
    ):
        result = await get_or_generate_summary(session, trial)

    assert result["summary"] == "fresh"
    session.execute.assert_awaited_once()


from api.services.summarize_trajectory import (
    SCHEMA_VERSION,
    TaskContext,
    build_task_context,
    get_or_generate_summary,
)


def _trial_with_task(*, task_name, reward, model, harbor_config=None):
    return SimpleNamespace(
        id="t-1",
        name="trial-0",
        trial_s3_key="trials/t-1/",
        reward=reward,
        model=model,
        harbor_config=harbor_config,
        task=SimpleNamespace(name=task_name),
    )


@pytest.mark.asyncio
async def test_build_task_context_pulls_all_fields():
    trial = _trial_with_task(
        task_name="solve_x", reward=0.75, model="claude-sonnet-4-6"
    )

    async def fake_instruction(trial_arg):
        return "Solve the puzzle."

    async def fake_verifier(trial_arg):
        return "PASS\n"

    with patch(
        "api.services.summarize_trajectory.read_trial_instruction",
        new=fake_instruction,
    ), patch(
        "api.services.summarize_trajectory.read_trial_verifier_output",
        new=fake_verifier,
    ):
        ctx = await build_task_context(trial)

    assert ctx == TaskContext(
        task_name="solve_x",
        instruction="Solve the puzzle.",
        final_reward=0.75,
        model_used="claude-sonnet-4-6",
        verifier_output="PASS\n",
    )


@pytest.mark.asyncio
async def test_build_task_context_handles_missing_fields():
    trial = _trial_with_task(task_name="solve_x", reward=None, model=None)

    async def fake_none(trial_arg):
        return None

    with patch(
        "api.services.summarize_trajectory.read_trial_instruction",
        new=fake_none,
    ), patch(
        "api.services.summarize_trajectory.read_trial_verifier_output",
        new=fake_none,
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
        task_name="solve_x",
        reward=None,
        model=None,
        harbor_config={"agent": {"model": "claude-sonnet-4-6"}},
    )

    async def fake_none(trial_arg):
        return None

    with patch(
        "api.services.summarize_trajectory.read_trial_instruction",
        new=fake_none,
    ), patch(
        "api.services.summarize_trajectory.read_trial_verifier_output",
        new=fake_none,
    ):
        ctx = await build_task_context(trial)

    assert ctx.model_used == "claude-sonnet-4-6"


def test_render_prompt_includes_task_block():
    from api.services.summarize_trajectory import _render_prompt

    trajectory = {"steps": [{"step_id": 1}]}
    ctx = TaskContext(
        task_name="solve_x",
        instruction="Do the thing.",
        final_reward=1.0,
        model_used="claude-sonnet-4-6",
        verifier_output="PASS\n",
    )
    prompt = _render_prompt(trajectory, ctx)
    assert "<task>" in prompt
    assert "Name: solve_x" in prompt
    assert "Instruction: Do the thing." in prompt
    assert "Final reward: 1.0" in prompt
    assert "Verifier output: PASS" in prompt
    assert "Model: claude-sonnet-4-6" in prompt
    assert "<trajectory>" in prompt


def test_render_prompt_marks_missing_fields_unavailable():
    from api.services.summarize_trajectory import _render_prompt

    ctx = TaskContext(
        task_name="solve_x",
        instruction=None,
        final_reward=None,
        model_used=None,
        verifier_output=None,
    )
    prompt = _render_prompt({"steps": []}, ctx)
    assert "Instruction: [unavailable]" in prompt
    assert "Final reward: [unavailable]" in prompt
    assert "Verifier output: [unavailable]" in prompt
    assert "Model: [unavailable]" in prompt


def test_render_prompt_truncates_long_instruction_and_verifier():
    from api.services.summarize_trajectory import (
        MAX_TEXT_CHARS,
        TRUNCATION_MARKER,
        _render_prompt,
    )

    long_text = "A" * (MAX_TEXT_CHARS + 500)
    ctx = TaskContext(
        task_name="t",
        instruction=long_text,
        final_reward=None,
        model_used=None,
        verifier_output=long_text,
    )
    prompt = _render_prompt({"steps": []}, ctx)
    # Both fields are truncated independently — the truncation marker
    # appears at least twice (once per truncated field).
    marker_prefix = TRUNCATION_MARKER.split("{")[0]
    assert prompt.count(marker_prefix) >= 2


def test_schema_version_is_two():
    from api.services.summarize_trajectory import SCHEMA_VERSION

    assert SCHEMA_VERSION == "2"


@pytest.mark.asyncio
async def test_get_or_generate_fetches_trajectory_and_context_in_parallel():
    import asyncio as _asyncio

    trial = _fake_trial(has_trajectory=True, trajectory_summary=None)
    session = _fake_session()

    started: list[str] = []
    finished: list[str] = []

    async def slow_trajectory(trial_arg):
        started.append("trajectory")
        await _asyncio.sleep(0.05)
        finished.append("trajectory")
        return {"steps": [{"step_id": 1}]}

    async def slow_context(trial_arg):
        started.append("context")
        await _asyncio.sleep(0.05)
        finished.append("context")
        return TaskContext(
            task_name="t",
            instruction=None,
            final_reward=None,
            model_used=None,
            verifier_output=None,
        )

    fresh = {
        "schema_version": SCHEMA_VERSION,
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-05-02T00:00:00Z",
        "summary": "ok",
        "highlights": [],
    }

    with patch(
        "api.services.summarize_trajectory.read_trial_trajectory",
        new=slow_trajectory,
    ), patch(
        "api.services.summarize_trajectory.build_task_context",
        new=slow_context,
    ), patch(
        "api.services.summarize_trajectory.generate",
        new_callable=AsyncMock,
        return_value=fresh,
    ):
        await get_or_generate_summary(session, trial)

    # Both started before either finished -> parallel
    assert {started[0], started[1]} == {"trajectory", "context"}
    assert len(finished) == 2
