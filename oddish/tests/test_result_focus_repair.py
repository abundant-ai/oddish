"""Unit tests for the LLM-backed result_focus repair (no real API calls)."""

from unittest.mock import AsyncMock

import pytest

from oddish.core import llm
from oddish.core.llm import LLMResult
from oddish.core.result_focus_repair import (
    repair_result_focus_if_needed,
    repair_result_focus_json,
)


def _fake_complete(monkeypatch, *texts: str, exc: Exception | None = None) -> AsyncMock:
    """Stand in for ``oddish.core.llm.complete``; one text per call."""
    side_effect = exc or [LLMResult(text=t, model="claude-haiku-4-5") for t in texts]
    fake = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(llm, "complete", fake)
    return fake


def _sent_prompt(fake: AsyncMock) -> str:
    return fake.await_args_list[0].kwargs["prompt"]


@pytest.mark.asyncio
async def test_repair_parses_clean_llm_json(monkeypatch):
    fake = _fake_complete(monkeypatch, '{"verdict": "string"}')
    obj, raw = await repair_result_focus_json('{"verdict": "string",}')
    assert obj == {"verdict": "string"}
    assert raw == '{"verdict": "string"}'
    assert fake.await_args_list[0].kwargs["handler"] == "result_focus_repair"


@pytest.mark.asyncio
async def test_repair_extracts_object_from_fenced_or_prose_output(monkeypatch):
    # Model wrapped the object in a code fence / prose; we still salvage it.
    _fake_complete(monkeypatch, 'Here you go:\n```json\n{"a": 1}\n```')
    obj, _ = await repair_result_focus_json("{'a': 1}")
    assert obj == {"a": 1}


@pytest.mark.asyncio
async def test_repair_llm_extraction_fallback_salvages_unparseable_output(monkeypatch):
    # First repair pass returns prose with no JSON at all -> deterministic
    # extraction fails -> a second LLM pass salvages the object.
    fake = _fake_complete(
        monkeypatch, "sorry, here is the spec", '{"verdict": "string"}'
    )
    obj, raw = await repair_result_focus_json('{"verdict": "string",}')
    assert obj == {"verdict": "string"}
    assert raw == "sorry, here is the spec"
    assert fake.await_count == 2


@pytest.mark.asyncio
async def test_repair_returns_none_when_both_passes_fail(monkeypatch):
    fake = _fake_complete(monkeypatch, "not json", "still not json")
    obj, _ = await repair_result_focus_json('{"verdict": "string",}')
    assert obj is None
    assert fake.await_count == 2


@pytest.mark.asyncio
async def test_repair_swallows_api_errors(monkeypatch):
    _fake_complete(monkeypatch, exc=RuntimeError("boom"))
    obj, raw = await repair_result_focus_json("{bad}")
    assert obj is None
    assert raw == ""


@pytest.mark.asyncio
async def test_if_needed_returns_repaired_json_string(monkeypatch):
    _fake_complete(monkeypatch, '{"verdict": "string"}')
    out = await repair_result_focus_if_needed('{"verdict": "string",}')
    assert out == '{"verdict": "string"}'


@pytest.mark.asyncio
async def test_if_needed_leaves_valid_json_untouched(monkeypatch):
    fake = _fake_complete(monkeypatch, "SHOULD NOT BE CALLED")
    valid = '{"verdict": "string"}'
    out = await repair_result_focus_if_needed(valid)
    assert out == valid
    assert fake.await_count == 0  # no LLM call for already-valid JSON


@pytest.mark.asyncio
async def test_if_needed_leaves_prose_question_untouched(monkeypatch):
    fake = _fake_complete(monkeypatch, "SHOULD NOT BE CALLED")
    prose = "Did the agent attempt reward hacking?"
    out = await repair_result_focus_if_needed(prose)
    assert out == prose
    assert fake.await_count == 0  # prose is not a JSON spec; no repair


@pytest.mark.asyncio
async def test_if_needed_falls_back_to_original_when_repair_fails(monkeypatch):
    _fake_complete(monkeypatch, "still not json", "still not json")
    malformed = '{"verdict": "string",}'
    out = await repair_result_focus_if_needed(malformed)
    assert out == malformed  # unchanged -> renderer does verbatim best-effort


@pytest.mark.asyncio
async def test_if_needed_repairs_array_intended_json(monkeypatch):
    # Leading "[" is JSON-intended (the leaf parser flags it as such), so a
    # malformed array spec is queued for repair, not silently treated as prose.
    fake = _fake_complete(monkeypatch, '{"items": [1, 2]}')
    out = await repair_result_focus_if_needed("[1, 2,]")
    assert out == '{"items": [1, 2]}'
    assert fake.await_count == 1


@pytest.mark.asyncio
async def test_kind_schema_uses_schema_repair_prompt(monkeypatch):
    fake = _fake_complete(monkeypatch, '{"type": "object"}')
    await repair_result_focus_json('{"type": "object",}', kind="schema")
    prompt = _sent_prompt(fake)
    assert "JSON Schema" in prompt
    assert "output specification" not in prompt


@pytest.mark.asyncio
async def test_kind_output_spec_uses_output_spec_repair_prompt(monkeypatch):
    fake = _fake_complete(monkeypatch, '{"verdict": "yes"}')
    await repair_result_focus_json('{"verdict": "yes",}', kind="output_spec")
    prompt = _sent_prompt(fake)
    assert "output specification" in prompt
    assert "JSON Schema" not in prompt


@pytest.mark.asyncio
async def test_default_kind_is_output_spec(monkeypatch):
    fake = _fake_complete(monkeypatch, '{"verdict": "yes"}')
    await repair_result_focus_json('{"verdict": "yes",}')
    assert "output specification" in _sent_prompt(fake)


@pytest.mark.asyncio
async def test_if_needed_threads_kind_to_repair_prompt(monkeypatch):
    fake = _fake_complete(monkeypatch, '{"type": "object"}')
    out = await repair_result_focus_if_needed('{"type": "object",}', kind="schema")
    assert out == '{"type": "object"}'
    assert "JSON Schema" in _sent_prompt(fake)
