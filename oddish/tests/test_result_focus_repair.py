"""Unit tests for the LLM-backed result_focus repair (no real API calls)."""

import pytest

from oddish.core.result_focus_repair import (
    repair_result_focus_if_needed,
    repair_result_focus_json,
)
from oddish.core.result_focus_schema import parse_result_focus


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


class _SeqMessages:
    """Returns a different text per ``create`` call (for multi-pass repair)."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeMessage(self._texts[len(self.calls) - 1])


class _SeqClient:
    def __init__(self, texts: list[str]) -> None:
        self.messages = _SeqMessages(texts)


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
async def test_repair_llm_extraction_fallback_salvages_unparseable_output():
    # First repair pass returns prose with no JSON at all -> deterministic
    # extraction fails -> a second LLM pass salvages the object.
    client = _SeqClient(["sorry, here is the spec", '{"verdict": "string"}'])
    obj, raw = await repair_result_focus_json('{"verdict": "string",}', client=client)
    assert obj == {"verdict": "string"}
    assert raw == "sorry, here is the spec"
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_repair_returns_none_when_both_passes_fail():
    client = _SeqClient(["not json", "still not json"])
    obj, _ = await repair_result_focus_json('{"verdict": "string",}', client=client)
    assert obj is None
    assert len(client.messages.calls) == 2


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


@pytest.mark.asyncio
async def test_if_needed_repairs_array_intended_json():
    # Leading "[" is JSON-intended (the leaf parser flags it as such), so a
    # malformed array spec is queued for repair, not silently treated as prose.
    client = _FakeClient(text='{"items": [1, 2]}')
    out = await repair_result_focus_if_needed("[1, 2,]", client=client)
    assert out == '{"items": [1, 2]}'
    assert len(client.messages.calls) == 1


# --- parse_result_focus: the repair-capable parser ---------------------------


@pytest.mark.asyncio
async def test_parse_returns_object_without_llm_for_valid_json():
    client = _FakeClient(text="SHOULD NOT BE CALLED")
    out = await parse_result_focus('{"verdict": "string"}', client=client)
    assert out == {"verdict": "string"}
    assert client.messages.calls == []  # valid JSON -> deterministic, no LLM


@pytest.mark.asyncio
async def test_parse_repairs_malformed_object():
    client = _FakeClient(text='{"verdict": "string"}')
    out = await parse_result_focus('{"verdict": "string",}', client=client)
    assert out == {"verdict": "string"}
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_parse_does_not_repair_valid_non_object():
    # "[1,2]" is valid JSON but not an object -> nothing to repair, no LLM call.
    client = _FakeClient(text="SHOULD NOT BE CALLED")
    assert await parse_result_focus("[1, 2]", client=client) is None
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_parse_does_not_repair_prose():
    client = _FakeClient(text="SHOULD NOT BE CALLED")
    assert await parse_result_focus("Did the agent cheat?", client=client) is None
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_parse_repair_false_skips_llm():
    client = _FakeClient(text='{"verdict": "string"}')
    assert await parse_result_focus('{"x": 1,}', client=client, repair=False) is None
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_parse_threads_kind_to_repair_prompt():
    client = _FakeClient(text='{"type": "object"}')
    await parse_result_focus('{"type": "object",}', client=client, kind="schema")
    assert "JSON Schema" in client.messages.calls[0]["messages"][0]["content"]


def _sent_prompt(client: _FakeClient) -> str:
    return client.messages.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_kind_schema_uses_schema_repair_prompt():
    client = _FakeClient(text='{"type": "object"}')
    await repair_result_focus_json('{"type": "object",}', client=client, kind="schema")
    prompt = _sent_prompt(client)
    assert "JSON Schema" in prompt
    assert "output specification" not in prompt


@pytest.mark.asyncio
async def test_kind_output_spec_uses_output_spec_repair_prompt():
    client = _FakeClient(text='{"verdict": "yes"}')
    await repair_result_focus_json(
        '{"verdict": "yes",}', client=client, kind="output_spec"
    )
    prompt = _sent_prompt(client)
    assert "output specification" in prompt
    assert "JSON Schema" not in prompt


@pytest.mark.asyncio
async def test_default_kind_is_output_spec():
    client = _FakeClient(text='{"verdict": "yes"}')
    await repair_result_focus_json('{"verdict": "yes",}', client=client)
    assert "output specification" in _sent_prompt(client)


@pytest.mark.asyncio
async def test_if_needed_threads_kind_to_repair_prompt():
    client = _FakeClient(text='{"type": "object"}')
    out = await repair_result_focus_if_needed(
        '{"type": "object",}', client=client, kind="schema"
    )
    assert out == '{"type": "object"}'
    assert "JSON Schema" in _sent_prompt(client)
