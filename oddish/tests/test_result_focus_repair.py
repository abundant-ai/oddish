"""Unit tests for the LLM-backed result_focus repair (no real API calls)."""

import pytest

from oddish.core import llm
from oddish.core.llm import LLMResult
from oddish.core.result_focus_repair import (
    repair_result_focus_if_needed,
    repair_result_focus_json,
)


def _result(text: str) -> LLMResult:
    return LLMResult(
        text=text,
        model="claude-haiku-4-5",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.0,
    )


class _FakeComplete:
    """Stands in for ``oddish.core.llm.complete``; one text per call."""

    def __init__(self, texts: list[str] | None = None, exc: Exception | None = None):
        self._texts = texts or []
        self._exc = exc
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        index = min(len(self.calls), len(self._texts)) - 1
        return _result(self._texts[index])


def _install(monkeypatch, fake: _FakeComplete) -> _FakeComplete:
    monkeypatch.setattr(llm, "complete", fake)
    return fake


def _sent_prompt(fake: _FakeComplete) -> str:
    return fake.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_repair_parses_clean_llm_json(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(['{"verdict": "string"}']))
    obj, raw = await repair_result_focus_json('{"verdict": "string",}')
    assert obj == {"verdict": "string"}
    assert raw == '{"verdict": "string"}'
    assert fake.calls[0]["handler"] == "result_focus_repair"


@pytest.mark.asyncio
async def test_repair_extracts_object_from_fenced_or_prose_output(monkeypatch):
    # Model wrapped the object in a code fence / prose; we still salvage it.
    _install(monkeypatch, _FakeComplete(['Here you go:\n```json\n{"a": 1}\n```']))
    obj, _ = await repair_result_focus_json("{'a': 1}")
    assert obj == {"a": 1}


@pytest.mark.asyncio
async def test_repair_llm_extraction_fallback_salvages_unparseable_output(monkeypatch):
    # First repair pass returns prose with no JSON at all -> deterministic
    # extraction fails -> a second LLM pass salvages the object.
    fake = _install(
        monkeypatch,
        _FakeComplete(["sorry, here is the spec", '{"verdict": "string"}']),
    )
    obj, raw = await repair_result_focus_json('{"verdict": "string",}')
    assert obj == {"verdict": "string"}
    assert raw == "sorry, here is the spec"
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_repair_returns_none_when_both_passes_fail(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(["not json", "still not json"]))
    obj, _ = await repair_result_focus_json('{"verdict": "string",}')
    assert obj is None
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_repair_swallows_api_errors(monkeypatch):
    _install(monkeypatch, _FakeComplete(exc=RuntimeError("boom")))
    obj, raw = await repair_result_focus_json("{bad}")
    assert obj is None
    assert raw == ""


@pytest.mark.asyncio
async def test_if_needed_returns_repaired_json_string(monkeypatch):
    _install(monkeypatch, _FakeComplete(['{"verdict": "string"}']))
    out = await repair_result_focus_if_needed('{"verdict": "string",}')
    assert out == '{"verdict": "string"}'


@pytest.mark.asyncio
async def test_if_needed_leaves_valid_json_untouched(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(["SHOULD NOT BE CALLED"]))
    valid = '{"verdict": "string"}'
    out = await repair_result_focus_if_needed(valid)
    assert out == valid
    assert fake.calls == []  # no LLM call for already-valid JSON


@pytest.mark.asyncio
async def test_if_needed_leaves_prose_question_untouched(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(["SHOULD NOT BE CALLED"]))
    prose = "Did the agent attempt reward hacking?"
    out = await repair_result_focus_if_needed(prose)
    assert out == prose
    assert fake.calls == []  # prose is not a JSON spec; no repair


@pytest.mark.asyncio
async def test_if_needed_falls_back_to_original_when_repair_fails(monkeypatch):
    _install(monkeypatch, _FakeComplete(["still not json"]))
    malformed = '{"verdict": "string",}'
    out = await repair_result_focus_if_needed(malformed)
    assert out == malformed  # unchanged -> renderer does verbatim best-effort


@pytest.mark.asyncio
async def test_if_needed_repairs_array_intended_json(monkeypatch):
    # Leading "[" is JSON-intended (the leaf parser flags it as such), so a
    # malformed array spec is queued for repair, not silently treated as prose.
    fake = _install(monkeypatch, _FakeComplete(['{"items": [1, 2]}']))
    out = await repair_result_focus_if_needed("[1, 2,]")
    assert out == '{"items": [1, 2]}'
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_kind_schema_uses_schema_repair_prompt(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(['{"type": "object"}']))
    await repair_result_focus_json('{"type": "object",}', kind="schema")
    prompt = _sent_prompt(fake)
    assert "JSON Schema" in prompt
    assert "output specification" not in prompt


@pytest.mark.asyncio
async def test_kind_output_spec_uses_output_spec_repair_prompt(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(['{"verdict": "yes"}']))
    await repair_result_focus_json('{"verdict": "yes",}', kind="output_spec")
    prompt = _sent_prompt(fake)
    assert "output specification" in prompt
    assert "JSON Schema" not in prompt


@pytest.mark.asyncio
async def test_default_kind_is_output_spec(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(['{"verdict": "yes"}']))
    await repair_result_focus_json('{"verdict": "yes",}')
    assert "output specification" in _sent_prompt(fake)


@pytest.mark.asyncio
async def test_if_needed_threads_kind_to_repair_prompt(monkeypatch):
    fake = _install(monkeypatch, _FakeComplete(['{"type": "object"}']))
    out = await repair_result_focus_if_needed('{"type": "object",}', kind="schema")
    assert out == '{"type": "object"}'
    assert "JSON Schema" in _sent_prompt(fake)
