"""Unit tests for the LLM-backed result_focus repair (no real API calls)."""

import pytest

from oddish.core.result_focus_repair import (
    repair_result_focus_if_needed,
    repair_result_focus_json,
)


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self._text = text
        self._exc = exc
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return _FakeMessage(self._text)


class _FakeClient:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.messages = _FakeMessages(text, exc)


@pytest.mark.asyncio
async def test_repair_parses_clean_llm_json():
    client = _FakeClient(text='{"verdict": "string"}')
    obj, raw = await repair_result_focus_json('{"verdict": "string",}', client=client)
    assert obj == {"verdict": "string"}
    assert raw == '{"verdict": "string"}'


@pytest.mark.asyncio
async def test_repair_extracts_object_from_fenced_or_prose_output():
    # Model wrapped the object in a code fence / prose; we still salvage it.
    client = _FakeClient(text='Here you go:\n```json\n{"a": 1}\n```')
    obj, _ = await repair_result_focus_json("{'a': 1}", client=client)
    assert obj == {"a": 1}


@pytest.mark.asyncio
async def test_repair_swallows_api_errors():
    client = _FakeClient(exc=RuntimeError("boom"))
    obj, raw = await repair_result_focus_json("{bad}", client=client)
    assert obj is None
    assert raw == ""


@pytest.mark.asyncio
async def test_if_needed_returns_repaired_json_string():
    client = _FakeClient(text='{"verdict": "string"}')
    out = await repair_result_focus_if_needed('{"verdict": "string",}', client=client)
    assert out == '{"verdict": "string"}'


@pytest.mark.asyncio
async def test_if_needed_leaves_valid_json_untouched():
    client = _FakeClient(text="SHOULD NOT BE CALLED")
    valid = '{"verdict": "string"}'
    out = await repair_result_focus_if_needed(valid, client=client)
    assert out == valid
    assert client.messages.calls == []  # no LLM call for already-valid JSON


@pytest.mark.asyncio
async def test_if_needed_leaves_prose_question_untouched():
    client = _FakeClient(text="SHOULD NOT BE CALLED")
    prose = "Did the agent attempt reward hacking?"
    out = await repair_result_focus_if_needed(prose, client=client)
    assert out == prose
    assert client.messages.calls == []  # prose is not a JSON spec; no repair


@pytest.mark.asyncio
async def test_if_needed_falls_back_to_original_when_repair_fails():
    client = _FakeClient(text="still not json")
    malformed = '{"verdict": "string",}'
    out = await repair_result_focus_if_needed(malformed, client=client)
    assert out == malformed  # unchanged -> renderer does verbatim best-effort
