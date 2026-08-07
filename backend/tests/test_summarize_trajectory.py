"""Tests for api.services.summarize_trajectory."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
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
async def test_generate_rejects_code_fences_outside_structured_output(monkeypatch):
    from api.services.summarize_trajectory import SummaryGenerationError, generate

    _patch_block_persistence(monkeypatch)
    body = json.dumps({"summary": "ok", "highlights": [], "components": []})
    _install_fake_llm(monkeypatch, _fake_llm(f"```json\n{body}\n```"))
    with pytest.raises(SummaryGenerationError):
        await generate(
            _trajectory_with_steps([1]),
            _minimal_ctx(),
            **_PROMPT_KWARGS,
        )


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
async def test_generate_wraps_template_read_errors(monkeypatch, tmp_path):
    """Block construction reads the packaged template from disk; a missing or
    unreadable file must surface as SummaryGenerationError, not raw OSError —
    callers (the trials summary route) only handle the former."""
    from api.services import summarize_trajectory
    from api.services.summarize_trajectory import SummaryGenerationError, generate

    _patch_block_persistence(monkeypatch)
    monkeypatch.setattr(
        summarize_trajectory, "_SUMMARY_PROMPT_PATH", tmp_path / "missing.txt"
    )
    with pytest.raises(SummaryGenerationError):
        await generate(_trajectory_with_steps([1]), _minimal_ctx())


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
async def test_get_or_generate_refresh_ignores_a_fresh_block():
    """refresh=True must regenerate even when a fresh block exists.

    Freshness is keyed on schema_version alone, so a stored summary can be
    "fresh" and still carry a retired taxonomy. Without this escape hatch
    those summaries are unreachable -- an analysis rerun goes through this
    same function and would hand back the stale block.
    """
    cached = {
        "schema_version": "5",
        "summary": "stale vocabulary",
        "highlights": [],
        "components": [],
    }
    regenerated = {**cached, "summary": "current vocabulary"}
    trial = _fake_trial(has_trajectory=True)
    session = _fake_session()
    with (
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new_callable=AsyncMock,
            return_value=cached,
        ) as load,
        patch(
            "api.services.summarize_trajectory.read_trial_trajectory",
            new_callable=AsyncMock,
            return_value={"steps": [_make_step(1)]},
        ),
        patch(
            "api.services.summarize_trajectory.build_task_context",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(
                task_name="t", instruction="i", final_reward="1",
                model_used="m", verifier_output="v",
            ),
        ),
        patch(
            "api.services.summarize_trajectory.generate",
            new_callable=AsyncMock,
            return_value=regenerated,
        ) as gen,
    ):
        result = await get_or_generate_summary(session, trial, refresh=True)

    assert result == regenerated
    gen.assert_awaited_once()
    # The cache is never consulted, before or inside the generation lock.
    load.assert_not_awaited()
    # The new summary is still mirrored into the trials column.
    session.commit.assert_awaited()


def test_summary_stamps_the_shared_schema_version():
    """to_summary must stamp the constant the freshness query compares to.

    A literal here would make a SCHEMA_VERSION bump unusable: every read would
    miss, regenerate, write the old version, and miss again forever.
    """
    from api.services.blocks.analyzer.trajectory import trajectory_component_block
    from api.services.summarize_trajectory import SCHEMA_VERSION

    source = Path(trajectory_component_block.__file__).read_text()
    assert '"schema_version": SCHEMA_VERSION' in source
    assert '"schema_version": "5"' not in source
    assert SCHEMA_VERSION == "5"


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
        result = await get_or_generate_summary(session, trial)
    assert result == fresh
    session.execute.assert_awaited_once()  # the mirror UPDATE
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

    fresh = {"schema_version": "5", "summary": "ok", "highlights": [], "components": []}

    with (
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new_callable=AsyncMock,
            return_value=None,
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


class _FakeAwaitableAttrs:
    """Mirror of SQLAlchemy's ``awaitable_attrs``: each attribute awaits to
    the underlying object's attribute (``build_task_context`` loads
    ``trial.task`` through it)."""

    def __init__(self, obj):
        self._obj = obj

    def __getattr__(self, name):
        async def _get():
            return getattr(self._obj, name)

        return _get()


def _trial_with_task(*, task_name, reward, model, harbor_config=None):
    trial = SimpleNamespace(
        id="t-1",
        name="trial-0",
        trial_s3_key="trials/t-1/",
        reward=reward,
        model=model,
        harbor_config=harbor_config,
        task=SimpleNamespace(name=task_name),
    )
    trial.awaitable_attrs = _FakeAwaitableAttrs(trial)
    return trial


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


def test_packaged_summary_prompt_template_has_taxonomy_placeholder():
    """The packaged template is the only prompt source now; it must ship with
    oddish and retain the ``{{taxonomy}}`` placeholder TrajectoryBlock renders."""
    from api.services.summarize_trajectory import load_summary_prompt_template

    content = load_summary_prompt_template()
    assert "{{taxonomy}}" in content


def test_build_summary_block_wires_structured_output_for_both_provider_paths():
    from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
        TrajectoryBlock,
        TrajectoryOutput,
    )
    from api.services.summarize_trajectory import build_summary_block

    block = build_summary_block(
        _trajectory_with_steps([1]),
        _minimal_ctx(),
        analyzer_id="tr_structured",
        model="claude-sonnet-4-6",
    )

    assert TrajectoryBlock.output_schema is TrajectoryOutput
    assert block._response_format is TrajectoryBlock.output_schema
    assert block._output_schema == TrajectoryBlock.output_schema.model_json_schema()


@pytest.mark.asyncio
async def test_build_summary_block_defaults_to_packaged_template(monkeypatch):
    """With no explicit template, the block's prompt is built from the
    packaged trajectory-summary file."""
    from api.services.summarize_trajectory import (
        build_summary_block,
        load_summary_prompt_template,
    )

    _patch_block_persistence(monkeypatch)
    block = build_summary_block(
        _trajectory_with_steps([1]),
        _minimal_ctx(),
        analyzer_id="tr_default",
        model="claude-sonnet-4-6",
    )
    # The packaged template's opening line survives into the built prompt
    # (the {{taxonomy}} placeholder itself is rendered away).
    first_line = load_summary_prompt_template().strip().splitlines()[0]
    assert first_line.split("{{")[0].strip() in block.prompt


# ---------------------------------------------------------------------------
# context-overflow clipping + shrink-and-retry
#
# Prod (2026-08-05): 473 trajectory_summary blocks in 30d died on a 400
# "prompt is too long", up to 11.2M tokens against a 1M cap. Characters do not
# predict tokens closely enough to preflight a static budget -- a 588k-char
# prompt overflowed while a 2.17M-char one fit -- so the API decides.
# ---------------------------------------------------------------------------


class _RecordingFakeLLM:
    """Fake client that records the prompt and can fail the first N calls."""

    def __init__(self, prompts: list[str], payload: str, failures: list):
        self._prompts = prompts
        self._payload = payload
        self._failures = failures
        self.last_system_prompt = None
        self.last_usage = None

    async def stream(self, prompt: str, *, system_prompt: str | None = None):
        self._prompts.append(prompt)
        if self._failures:
            raise self._failures.pop(0)
        yield self._payload

    async def aclose(self) -> None:
        return None


def _install_recording_llm(monkeypatch, payload: str, failures: list) -> list[str]:
    prompts: list[str] = []

    async def create(*args, **kwargs):
        return _RecordingFakeLLM(prompts, payload, failures)

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.create_llm_client", create
    )
    return prompts


_OVERFLOW = Exception(
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'prompt is too long: "
    "1744777 tokens > 1000000 maximum'}}"
)


def test_clip_trajectory_steps_keeps_head_and_tail():
    from api.services.summarize_trajectory import clip_trajectory_steps

    clipped = clip_trajectory_steps(_trajectory_with_steps(list(range(1, 21))), 6)
    kept = [s["step_id"] for s in clipped["steps"] if s.get("step_id") is not None]
    assert kept == [1, 2, 3, 18, 19, 20]


def test_clip_trajectory_steps_marks_the_omission():
    from api.services.summarize_trajectory import clip_trajectory_steps

    clipped = clip_trajectory_steps(_trajectory_with_steps(list(range(1, 21))), 6)
    markers = [s for s in clipped["steps"] if s.get("step_id") is None]
    assert len(markers) == 1
    assert "14 steps omitted" in markers[0]["message"]


def test_clip_trajectory_steps_marker_carries_last_dropped_timestamp():
    from api.services.summarize_trajectory import clip_trajectory_steps

    steps = [
        _make_step(i, timestamp=f"2026-04-30T12:{i:02d}:00Z") for i in range(1, 21)
    ]
    clipped = clip_trajectory_steps({"steps": steps}, 6)
    marker = next(s for s in clipped["steps"] if s.get("step_id") is None)
    # Tail is steps 18-20, so step 17 is the last one dropped.
    assert marker["timestamp"] == "2026-04-30T12:17:00Z"


def test_clipped_tail_duration_measures_against_the_dropped_predecessor():
    """A clipped summary must not report the first tail step as taking 0ms."""
    from pathlib import Path

    from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
        TrajectoryBlock,
        TrajectoryInput,
    )
    from api.services.summarize_trajectory import clip_trajectory_steps

    template = (
        Path(__file__).resolve().parents[2]
        / "oddish" / "src" / "oddish" / "analyze" / "prompts" / "trajectory_summary.txt"
    ).read_text()
    steps = [
        _make_step(i, timestamp=f"2026-04-30T12:{i:02d}:00Z") for i in range(1, 21)
    ]
    clipped = clip_trajectory_steps({"steps": steps}, 6)
    raw = json.dumps({
        "summary": "s",
        "highlights": [],
        "components": [
            {"step_ids": [18], "trajectory_component": "debugging", "summary": "y"}
        ],
    })
    block = TrajectoryBlock(
        TrajectoryInput(
            task_name="t",
            instruction=None,
            final_reward=None,
            model_used=None,
            verifier_output=None,
            trajectory=clipped,
        ),
        instructions_template=template,
    )
    out = block.to_summary(raw, model="claude-x")
    # Step 18 follows step 17 by one minute; the omission marker between them
    # must not flatten that to 0.
    assert out["components"][0]["duration_ms"] == 60_000


def test_clip_trajectory_steps_is_a_noop_under_budget():
    from api.services.summarize_trajectory import clip_trajectory_steps

    trajectory = _trajectory_with_steps([1, 2, 3])
    assert clip_trajectory_steps(trajectory, 10) == trajectory


def test_clip_trajectory_steps_does_not_mutate_its_input():
    from api.services.summarize_trajectory import clip_trajectory_steps

    trajectory = _trajectory_with_steps(list(range(1, 21)))
    clip_trajectory_steps(trajectory, 4)
    assert len(trajectory["steps"]) == 20


@pytest.mark.asyncio
async def test_generate_retries_with_fewer_steps_on_context_overflow(monkeypatch):
    from api.services.summarize_trajectory import generate

    _patch_block_persistence(monkeypatch)
    payload = json.dumps({"summary": "ok", "highlights": [], "components": []})
    prompts = _install_recording_llm(monkeypatch, payload, [_OVERFLOW])

    result = await generate(
        _trajectory_with_steps(list(range(1, 41))),
        _minimal_ctx(),
        **_PROMPT_KWARGS,
    )

    assert result["summary"] == "ok"
    assert len(prompts) == 2, "should retry exactly once after the overflow"
    assert len(prompts[1]) < len(prompts[0]), "the retry must send a smaller prompt"
    assert "steps omitted" in prompts[1]


@pytest.mark.asyncio
async def test_generate_halves_repeatedly_until_it_fits(monkeypatch):
    from api.services.summarize_trajectory import generate

    _patch_block_persistence(monkeypatch)
    payload = json.dumps({"summary": "ok", "highlights": [], "components": []})
    prompts = _install_recording_llm(
        monkeypatch, payload, [_OVERFLOW, _OVERFLOW, _OVERFLOW]
    )

    result = await generate(
        _trajectory_with_steps(list(range(1, 41))),
        _minimal_ctx(),
        **_PROMPT_KWARGS,
    )

    assert result["summary"] == "ok"
    assert len(prompts) == 4
    sizes = [len(p) for p in prompts]
    assert sizes == sorted(sizes, reverse=True), f"each attempt must shrink: {sizes}"


@pytest.mark.asyncio
async def test_generate_does_not_retry_non_overflow_errors(monkeypatch):
    from api.services.summarize_trajectory import SummaryGenerationError, generate

    _patch_block_persistence(monkeypatch)
    payload = json.dumps({"summary": "ok", "highlights": [], "components": []})
    prompts = _install_recording_llm(
        monkeypatch, payload, [Exception("Error code: 529 - overloaded_error")]
    )

    with pytest.raises(SummaryGenerationError):
        await generate(
            _trajectory_with_steps(list(range(1, 41))),
            _minimal_ctx(),
            **_PROMPT_KWARGS,
        )
    assert len(prompts) == 1, "a non-overflow failure must not be retried"


@pytest.mark.asyncio
async def test_generate_gives_up_after_the_attempt_ceiling(monkeypatch):
    from api.services.summarize_trajectory import (
        MAX_OVERFLOW_ATTEMPTS,
        SummaryGenerationError,
        generate,
    )

    _patch_block_persistence(monkeypatch)
    payload = json.dumps({"summary": "ok", "highlights": [], "components": []})
    prompts = _install_recording_llm(
        monkeypatch, payload, [_OVERFLOW] * (MAX_OVERFLOW_ATTEMPTS + 3)
    )

    with pytest.raises(SummaryGenerationError) as excinfo:
        await generate(
            _trajectory_with_steps(list(range(1, 41))),
            _minimal_ctx(),
            **_PROMPT_KWARGS,
        )
    assert "prompt is too long" in str(excinfo.value)
    assert len(prompts) <= MAX_OVERFLOW_ATTEMPTS
