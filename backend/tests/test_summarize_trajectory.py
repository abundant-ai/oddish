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

_PROMPT_KWARGS = {
    "prompt_template": "INSTRUCTIONS {{taxonomy}}",
    "prompt_version": 1,
    "prompt_id": "prompt-test-id",
}


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
                        {
                            "type": "image",
                            "source": {"media_type": "image/png", "path": "y.png"},
                        },
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
    step = _make_step(
        1, observation={"results": [{"source_call_id": "c1", "content": huge}]}
    )
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
        task_name="test_task",
        instruction=None,
        final_reward=None,
        model_used=None,
        verifier_output=None,
    )


def _fake_llm(payload: str):
    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient

    return FakeAnalyzerLLMClient(chunks=[payload])


def _install_fake_llm(monkeypatch, client):
    async def create(*args, **kwargs):
        return client

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.create_llm_client", create
    )


def _patch_block_persistence(monkeypatch):
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock

    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", AsyncMock())
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", AsyncMock())


@pytest.mark.asyncio
async def test_generate_returns_persistable_summary(monkeypatch):
    from api.services.summarize_trajectory import generate

    _patch_block_persistence(monkeypatch)
    payload = json.dumps(
        {
            "summary": "Agent reproduced and fixed a flaky test.",
            "highlights": [
                {"step_id": 1, "title": "Repro", "why": "First."},
                {"step_id": 3, "title": "Fix", "why": "Patch."},
            ],
            "components": [
                {
                    "step_ids": [1, 2],
                    "trajectory_component": "debugging",
                    "summary": "d",
                }
            ],
        }
    )
    _install_fake_llm(monkeypatch, _fake_llm(payload))
    result = await generate(
        _trajectory_with_steps([1, 2, 3]),
        _minimal_ctx(),
        **_PROMPT_KWARGS,
    )
    assert result["schema_version"] == SCHEMA_VERSION == "5"
    assert result["summary"].startswith("Agent reproduced")
    assert [h["step_id"] for h in result["highlights"]] == [1, 3]
    assert result["components"][0]["trajectory_component"] == "debugging"
    assert "phases" not in result


@pytest.mark.asyncio
async def test_generate_drops_highlights_with_unknown_step_ids(monkeypatch):
    from api.services.summarize_trajectory import generate

    _patch_block_persistence(monkeypatch)
    payload = json.dumps(
        {
            "summary": "x",
            "highlights": [
                {"step_id": 1, "title": "ok", "why": "ok"},
                {"step_id": 999, "title": "bogus", "why": "hallucinated"},
            ],
            "components": [],
        }
    )
    _install_fake_llm(monkeypatch, _fake_llm(payload))
    result = await generate(
        _trajectory_with_steps([1, 2, 3]),
        _minimal_ctx(),
        **_PROMPT_KWARGS,
    )
    assert [h["step_id"] for h in result["highlights"]] == [1]


@pytest.mark.asyncio
async def test_generate_strips_code_fences_around_json(monkeypatch):
    from api.services.summarize_trajectory import generate

    _patch_block_persistence(monkeypatch)
    body = json.dumps({"summary": "ok", "highlights": [], "components": []})
    _install_fake_llm(monkeypatch, _fake_llm(f"```json\n{body}\n```"))
    result = await generate(
        _trajectory_with_steps([1]),
        _minimal_ctx(),
        **_PROMPT_KWARGS,
    )
    assert result["summary"] == "ok"


@pytest.mark.asyncio
async def test_generate_raises_on_malformed_json(monkeypatch):
    from api.services.summarize_trajectory import SummaryGenerationError, generate

    _patch_block_persistence(monkeypatch)
    _install_fake_llm(monkeypatch, _fake_llm("not json"))
    with pytest.raises(SummaryGenerationError):
        await generate(_trajectory_with_steps([1]), _minimal_ctx(), **_PROMPT_KWARGS)


@pytest.mark.asyncio
async def test_generate_raises_when_model_returns_non_object_json(monkeypatch):
    from api.services.summarize_trajectory import SummaryGenerationError, generate

    _patch_block_persistence(monkeypatch)
    _install_fake_llm(monkeypatch, _fake_llm("[1,2,3]"))
    with pytest.raises(SummaryGenerationError):
        await generate(_trajectory_with_steps([1]), _minimal_ctx(), **_PROMPT_KWARGS)


@pytest.mark.asyncio
async def test_generate_wraps_client_errors(monkeypatch):
    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summarize_trajectory import SummaryGenerationError, generate

    _patch_block_persistence(monkeypatch)
    client = FakeAnalyzerLLMClient(chunks=[], exc=RuntimeError("boom"))
    _install_fake_llm(monkeypatch, client)
    with pytest.raises(SummaryGenerationError):
        await generate(_trajectory_with_steps([1]), _minimal_ctx(), **_PROMPT_KWARGS)


@pytest.mark.asyncio
async def test_generate_returns_components(monkeypatch):
    from api.services.summarize_trajectory import generate

    _patch_block_persistence(monkeypatch)
    payload = json.dumps(
        {
            "summary": "s",
            "highlights": [],
            "components": [
                {
                    "step_ids": [1, 2],
                    "trajectory_component": "reading_files",
                    "summary": "look",
                },
                {
                    "step_ids": [3],
                    "trajectory_component": "implementing",
                    "summary": "code",
                },
            ],
        }
    )
    _install_fake_llm(monkeypatch, _fake_llm(payload))
    result = await generate(
        _trajectory_with_steps([1, 2, 3]),
        _minimal_ctx(),
        **_PROMPT_KWARGS,
    )
    assert [c["trajectory_component"] for c in result["components"]] == [
        "reading_files",
        "implementing",
    ]


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
        org_id=None,
        billed_user_id=None,
        experiment_id=None,
        task_id=None,
    )


def _fake_session():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_get_or_generate_returns_block_when_fresh():
    cached = {
        "schema_version": "5",
        "summary": "cached",
        "highlights": [],
        "components": [],
    }
    trial = _fake_trial(has_trajectory=True)
    session = _fake_session()
    with (
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new_callable=AsyncMock,
            return_value=cached,
        ),
        patch(
            "api.services.summarize_trajectory.generate",
            new_callable=AsyncMock,
        ) as gen,
    ):
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
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await get_or_generate_summary(session, trial)
    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_generate_persists_on_miss():
    trial = _fake_trial(has_trajectory=True)
    session = _fake_session()
    fresh = {
        "schema_version": "5",
        "summary": "fresh",
        "highlights": [],
        "components": [],
    }

    async def fake_traj(_t):
        return {"steps": [{"step_id": 1}]}

    async def fake_ctx(_t):
        return _minimal_ctx()

    with (
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "api.services.summarize_trajectory._load_summary_prompt",
            new_callable=AsyncMock,
            return_value=("INSTRUCTIONS {{taxonomy}}", 1, "prompt-test-id"),
        ),
        patch(
            "api.services.summarize_trajectory.read_trial_trajectory",
            new=fake_traj,
        ),
        patch(
            "api.services.summarize_trajectory.build_task_context",
            new=fake_ctx,
        ),
        patch(
            "api.services.summarize_trajectory.generate",
            new_callable=AsyncMock,
            return_value=fresh,
        ) as gen,
    ):
        result = await get_or_generate_summary(session, trial)
    assert result == fresh
    session.execute.assert_awaited_once()  # the mirror UPDATE
    session.commit.assert_awaited_once()
    assert gen.await_args.kwargs["prompt_id"] == "prompt-test-id"


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

    fresh = {"schema_version": "5", "summary": "ok", "highlights": [], "components": []}

    with (
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "api.services.summarize_trajectory._load_summary_prompt",
            new_callable=AsyncMock,
            return_value=("INSTRUCTIONS {{taxonomy}}", 1, "prompt-test-id"),
        ),
        patch(
            "api.services.summarize_trajectory.read_trial_trajectory",
            new=slow_trajectory,
        ),
        patch(
            "api.services.summarize_trajectory.build_task_context",
            new=slow_context,
        ),
        patch(
            "api.services.summarize_trajectory.generate",
            new_callable=AsyncMock,
            return_value=fresh,
        ),
    ):
        await get_or_generate_summary(session, trial)

    assert {started[0], started[1]} == {"trajectory", "context"}
    assert len(finished) == 2


# ---------------------------------------------------------------------------
# build_task_context
# ---------------------------------------------------------------------------


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

    async def fake_instruction(_t):
        return "Solve the puzzle."

    async def fake_verifier(_t):
        return "PASS\n"

    with (
        patch(
            "api.services.summarize_trajectory.read_trial_instruction",
            new=fake_instruction,
        ),
        patch(
            "api.services.summarize_trajectory.read_trial_verifier_output",
            new=fake_verifier,
        ),
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

    async def fake_none(_t):
        return None

    with (
        patch(
            "api.services.summarize_trajectory.read_trial_instruction",
            new=fake_none,
        ),
        patch(
            "api.services.summarize_trajectory.read_trial_verifier_output",
            new=fake_none,
        ),
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

    async def fake_none(_t):
        return None

    with (
        patch(
            "api.services.summarize_trajectory.read_trial_instruction",
            new=fake_none,
        ),
        patch(
            "api.services.summarize_trajectory.read_trial_verifier_output",
            new=fake_none,
        ),
    ):
        ctx = await build_task_context(trial)

    assert ctx.model_used == "claude-sonnet-4-6"


def test_schema_version_is_five():
    assert SCHEMA_VERSION == "5"


# ---------------------------------------------------------------------------
# build_summary_block (shared construction site)
# ---------------------------------------------------------------------------


class _RecordingLLM:
    """Fake client that records the prompt it was handed."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.prompt: str | None = None

    async def stream(self, prompt: str, *, system_prompt: str | None = None):
        self.prompt = prompt
        yield self._payload

    async def aclose(self) -> None:
        return None


def _summary_payload() -> str:
    return json.dumps(
        {
            "summary": "Agent fixed the bug.",
            "highlights": [{"step_id": 1, "title": "Repro", "why": "First."}],
            "components": [
                {"step_ids": [1], "trajectory_component": "debugging", "summary": "d"}
            ],
        }
    )


@pytest.mark.asyncio
async def test_generate_and_build_summary_block_agree(monkeypatch):
    """generate() must build its block through build_summary_block(), so the
    harness and production cannot drift in prompt, model, or metadata."""
    import api.services.summarize_trajectory as summarize_trajectory_module
    from api.services.summarize_trajectory import (
        build_summary_block,
        generate,
        resolve_summary_model,
    )

    _patch_block_persistence(monkeypatch)
    trajectory = _trajectory_with_steps([1, 2])
    ctx = _minimal_ctx()

    # Spy on the module-level call site so the assertions below check what
    # generate() actually passed, not what the test independently constructs
    # (which would be self-consistent regardless of generate()'s behavior).
    real_build_summary_block = build_summary_block
    captured: dict = {}

    def _spying_build_summary_block(*args, **kwargs):
        captured["kwargs"] = kwargs
        captured["block"] = real_build_summary_block(*args, **kwargs)
        return captured["block"]

    monkeypatch.setattr(
        summarize_trajectory_module, "build_summary_block", _spying_build_summary_block
    )

    recorder = _RecordingLLM(_summary_payload())
    _install_fake_llm(monkeypatch, recorder)
    await generate(deepcopy(trajectory), ctx, analyzer_id="tr_x", **_PROMPT_KWARGS)

    block = build_summary_block(
        deepcopy(trajectory),
        ctx,
        analyzer_id="tr_x",
        model=resolve_summary_model(),
        **_PROMPT_KWARGS,
    )

    assert recorder.prompt == block.prompt
    assert captured["kwargs"]["model"] == resolve_summary_model()
    assert captured["kwargs"]["analyzer_id"] == "tr_x"
    assert captured["block"].block_metadata["schema_version"] == SCHEMA_VERSION
    assert captured["block"].block_metadata["model"] == resolve_summary_model()
    assert captured["block"].block_metadata["prompt_key"] == "TRAJECTORY_SUMMARY"
    assert captured["block"].block_metadata["prompt_version"] == 1
    assert captured["block"].block_metadata["prompt_id"] == "prompt-test-id"


@pytest.mark.asyncio
async def test_load_summary_prompt_recovers_from_lost_seed_race(monkeypatch):
    """A losing first-seed request must keep the caller's ORM state usable."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from api.services.summarize_trajectory import (
        SUMMARY_PROMPT_KEY,
        _load_summary_prompt,
    )
    from oddish.core.prompt_seeds import seed_prompts as real_seed_prompts
    from oddish.db import PromptKind, PromptModel, get_session

    async with get_session() as session:
        await session.execute(
            PromptModel.__table__.delete().where(PromptModel.kind == SUMMARY_PROMPT_KEY)
        )
        await session.commit()

    async def fake_seed(_session):
        async with get_session() as winner:
            await real_seed_prompts(winner)
        raise IntegrityError(
            "insert", {}, Exception("duplicate key value violates unique constraint")
        )

    monkeypatch.setattr("api.services.summarize_trajectory.seed_prompts", fake_seed)

    async with get_session() as session:
        sentinel = (
            await session.execute(
                select(PromptModel).where(
                    PromptModel.kind == PromptKind.QA_PRE_TRIAL.value
                )
            )
        ).scalar_one()
        content, version, _prompt_id = await _load_summary_prompt(session)
        assert sentinel.kind == PromptKind.QA_PRE_TRIAL.value

    assert version == 1
    assert "{{taxonomy}}" in content


# ---------------------------------------------------------------------------
# _load_summary_prompt / get_or_generate_summary scope resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_summary_prompt_prefers_task_scoped_override():
    """Guard: a task-scoped TRAJECTORY_SUMMARY row must beat the global
    prompt when the caller's scope matches it. Exercises resolve_prompt_core
    for real against the DB."""
    import uuid

    from api.services.summarize_trajectory import (
        SUMMARY_PROMPT_KEY,
        _load_summary_prompt,
    )
    from oddish.core.prompts import set_prompt_core
    from oddish.db import PromptModel, get_session

    task_id = f"test_summary_scope_{uuid.uuid4().hex[:8]}"
    org_id = "org_summary_scope_test"

    async with get_session() as session:
        scoped_version = await set_prompt_core(
            session,
            kind=SUMMARY_PROMPT_KEY,
            content="SCOPED TASK PROMPT {{taxonomy}}",
            scope_type="task",
            scope_id=task_id,
            org_id=org_id,
        )
        await session.commit()
        scoped_prompt_id = scoped_version.prompt_id

    try:
        async with get_session() as session:
            content, _version, prompt_id = await _load_summary_prompt(
                session,
                org_id=org_id,
                user_id=None,
                experiment_id=None,
                task_id=task_id,
                trial_id=None,
            )
        assert content == "SCOPED TASK PROMPT {{taxonomy}}"
        assert prompt_id == scoped_prompt_id
    finally:
        async with get_session() as session:
            await session.execute(
                PromptModel.__table__.delete().where(
                    PromptModel.kind == SUMMARY_PROMPT_KEY,
                    PromptModel.scope_type == "task",
                    PromptModel.scope_id == task_id,
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_load_summary_prompt_falls_back_to_global_with_no_scoped_rows():
    """Behavior preservation: with no scoped rows for this context, the
    global prompt is still resolved -- the property that must not regress."""
    from api.services.summarize_trajectory import (
        SUMMARY_PROMPT_KEY,
        _load_summary_prompt,
    )
    from oddish.core.prompts import get_prompt_core
    from oddish.db import get_session

    async with get_session() as session:
        global_prompt, global_version = await get_prompt_core(
            session, SUMMARY_PROMPT_KEY
        )
        content, version, prompt_id = await _load_summary_prompt(
            session,
            org_id="org_with_no_overrides",
            user_id="user_with_no_overrides",
            experiment_id="exp_with_no_overrides",
            task_id="task_with_no_overrides",
            trial_id="trial_with_no_overrides",
        )

    assert content == global_version.content
    assert version == global_version.version
    assert prompt_id == global_prompt.id


@pytest.mark.asyncio
async def test_get_or_generate_summary_passes_trial_scope_to_prompt_loader():
    """get_or_generate_summary must thread the trial's own org/user/
    experiment/task/trial ids into the prompt lookup, not resolve globally."""
    trial = SimpleNamespace(
        id="t-scope-1",
        name="trial-0",
        trial_s3_key="trials/t-scope-1/",
        has_trajectory=True,
        agent="claude-code",
        finished_at=None,
        org_id="org-1",
        billed_user_id="user-1",
        experiment_id="exp-1",
        task_id="task-1",
    )
    session = _fake_session()
    fresh = {"schema_version": "5", "summary": "ok", "highlights": [], "components": []}

    async def fake_traj(_t):
        return {"steps": [{"step_id": 1}]}

    async def fake_ctx(_t):
        return _minimal_ctx()

    load_prompt = AsyncMock(
        return_value=("INSTRUCTIONS {{taxonomy}}", 1, "prompt-test-id")
    )

    with (
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "api.services.summarize_trajectory._load_summary_prompt",
            new=load_prompt,
        ),
        patch(
            "api.services.summarize_trajectory.read_trial_trajectory",
            new=fake_traj,
        ),
        patch(
            "api.services.summarize_trajectory.build_task_context",
            new=fake_ctx,
        ),
        patch(
            "api.services.summarize_trajectory.generate",
            new_callable=AsyncMock,
            return_value=fresh,
        ),
    ):
        await get_or_generate_summary(session, trial)

    load_prompt.assert_awaited_once_with(
        session,
        org_id="org-1",
        user_id="user-1",
        experiment_id="exp-1",
        task_id="task-1",
        trial_id="t-scope-1",
    )


# ---------------------------------------------------------------------------
# Guard: a scoped override's block run must attribute to itself, never to the
# global row's NULL-prompt_id fallback (finding 2 -- permanent counter
# pollution if this regresses).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_prompt_block_attributes_usage_to_override_not_global(
    monkeypatch,
):
    """End-to-end through generate()/build_summary_block() with a real DB: a
    task-scoped TRAJECTORY_SUMMARY override must produce an analyzer_blocks
    row stamped with the override's prompt_id, counted in the override's
    usage and NOT in the global row's usage."""
    import uuid

    from sqlalchemy import select

    from api.services.summarize_trajectory import SUMMARY_PROMPT_KEY, generate
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock
    from oddish.core.prompts import (
        get_prompt_core,
        get_prompt_usage_core,
        set_prompt_core,
    )
    from oddish.db import PromptModel, get_session
    from oddish.db.models import AnalyzerBlockModel

    task_id = f"test_summary_usage_{uuid.uuid4().hex[:8]}"
    org_id = "org_summary_usage_test"
    analyzer_id = f"tr_usage_guard_{uuid.uuid4().hex[:8]}"

    async with get_session() as session:
        scoped_version = await set_prompt_core(
            session,
            kind=SUMMARY_PROMPT_KEY,
            content="SCOPED USAGE {{taxonomy}}",
            scope_type="task",
            scope_id=task_id,
            org_id=org_id,
        )
        await session.commit()
        scoped_prompt_id = scoped_version.prompt_id

    async with get_session() as session:
        global_prompt, _ = await get_prompt_core(session, SUMMARY_PROMPT_KEY)
        global_usage_before = await get_prompt_usage_core(session, global_prompt.id)

    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", AsyncMock())
    payload = json.dumps({"summary": "s", "highlights": [], "components": []})
    _install_fake_llm(monkeypatch, _fake_llm(payload))

    try:
        await generate(
            _trajectory_with_steps([1]),
            _minimal_ctx(),
            analyzer_id=analyzer_id,
            prompt_template="SCOPED USAGE {{taxonomy}}",
            prompt_version=scoped_version.version,
            prompt_id=scoped_prompt_id,
        )

        async with get_session() as session:
            row = (
                await session.execute(
                    select(AnalyzerBlockModel).where(
                        AnalyzerBlockModel.analyzer_id == analyzer_id
                    )
                )
            ).scalar_one()
            assert row.prompt_id == scoped_prompt_id

            scoped_usage = await get_prompt_usage_core(session, scoped_prompt_id)
            assert scoped_usage["total"] == 1

            global_usage_after = await get_prompt_usage_core(
                session, global_prompt.id
            )
            assert global_usage_after["total"] == global_usage_before["total"]
    finally:
        async with get_session() as session:
            await session.execute(
                AnalyzerBlockModel.__table__.delete().where(
                    AnalyzerBlockModel.analyzer_id == analyzer_id
                )
            )
            await session.execute(
                PromptModel.__table__.delete().where(
                    PromptModel.kind == SUMMARY_PROMPT_KEY,
                    PromptModel.scope_type == "task",
                    PromptModel.scope_id == task_id,
                )
            )
            await session.commit()
