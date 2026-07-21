from types import SimpleNamespace

import pytest

from api.services.blocks.analyzer.analyzer_llm_client import (
    LLMClientType,
    FakeAnalyzerLLMClient,
    ApiAnalyzerLLMClient,
    OutputBudgetExceeded,
)


async def _collect(client, prompt):
    out = []
    async for chunk in client.stream(prompt):
        out.append(chunk)
    return out


class _AStream:
    """Stands in for the SDK's streaming context manager."""

    def __init__(self, parts, stop_reason="end_turn"):
        self._parts = parts
        self._stop_reason = stop_reason

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for p in self._parts:
                yield p

        return gen()

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self._stop_reason, usage=None)


def _fake_anthropic(stream_factory, recorder=None):
    """Build a fake AsyncAnthropic whose .messages.stream records its kwargs."""

    class _FakeMessages:
        def stream(self, **kwargs):
            if recorder is not None:
                recorder.update(kwargs)
            return stream_factory()

    class _FakeAnthropic:
        def __init__(self, *a, **k):
            self.messages = _FakeMessages()

        async def close(self):
            pass

    return _FakeAnthropic


@pytest.mark.asyncio
async def test_fake_client_yields_chunks():
    client = FakeAnalyzerLLMClient(chunks=["a", "b", "c"])
    assert await _collect(client, "p") == ["a", "b", "c"]
    await client.aclose()


@pytest.mark.asyncio
async def test_fake_client_raises_when_configured():
    client = FakeAnalyzerLLMClient(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await _collect(client, "p")


@pytest.mark.asyncio
async def test_api_client_streams_text_deltas(monkeypatch):
    # Fake the AsyncAnthropic streaming context manager.
    class _FakeMessages:
        def stream(self, **kwargs):
            assert kwargs["model"] == "claude-opus-4-8"
            assert kwargs["messages"][0]["content"] == "hi"
            return _AStream(["Hel", "lo"])

    class _FakeAnthropic:
        def __init__(self, *a, **k):
            self.messages = _FakeMessages()

        async def close(self):
            pass

    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client.AsyncAnthropic", _FakeAnthropic
    )
    client = ApiAnalyzerLLMClient()
    assert await _collect(client, "hi") == ["Hel", "lo"]
    await client.aclose()


@pytest.mark.asyncio
async def test_api_client_pins_thinking_off_by_default(monkeypatch):
    """Thinking shares the max_tokens ceiling with the JSON body, so leaving it
    to the model default silently truncates analyzer output on models that
    think by default (sonnet-5+). The request must say so explicitly."""
    sent: dict = {}
    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _fake_anthropic(lambda: _AStream(["{}"]), recorder=sent),
    )
    client = ApiAnalyzerLLMClient(model="claude-sonnet-5", max_tokens=2048)
    await _collect(client, "hi")
    await client.aclose()

    assert sent["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_api_client_thinking_override_is_forwarded(monkeypatch):
    sent: dict = {}
    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _fake_anthropic(lambda: _AStream(["{}"]), recorder=sent),
    )
    client = ApiAnalyzerLLMClient(thinking={"type": "adaptive"})
    await _collect(client, "hi")
    await client.aclose()

    assert sent["thinking"] == {"type": "adaptive"}


@pytest.mark.asyncio
async def test_api_client_raises_when_output_budget_exhausted(monkeypatch):
    """A truncated reply ends text_stream exactly like a complete one. Without
    the stop_reason check the half-written JSON reaches the block parser and
    surfaces only as an unexplained 'non-JSON output' error."""
    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _fake_anthropic(
            lambda: _AStream(['{"summary": "abc'], stop_reason="max_tokens")
        ),
    )
    client = ApiAnalyzerLLMClient(model="claude-sonnet-5", max_tokens=2048)
    with pytest.raises(OutputBudgetExceeded, match="max_tokens=2048"):
        await _collect(client, "hi")
    await client.aclose()


@pytest.mark.asyncio
async def test_api_client_does_not_raise_on_normal_stop(monkeypatch):
    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _fake_anthropic(lambda: _AStream(["{}"], stop_reason="end_turn")),
    )
    client = ApiAnalyzerLLMClient()
    assert await _collect(client, "hi") == ["{}"]
    await client.aclose()


@pytest.mark.asyncio
async def test_api_client_aclose_closes_inner(monkeypatch):
    closed = {"n": 0}

    class _FakeAnthropic:
        def __init__(self, *a, **k):
            self.messages = None

        async def close(self):
            closed["n"] += 1

    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client.AsyncAnthropic", _FakeAnthropic
    )
    client = ApiAnalyzerLLMClient()
    await client.aclose()
    assert closed["n"] == 1


from api.services.blocks.analyzer.analyzer_llm_client import (
    SandboxAnalyzerLLMClient,
    create_llm_client,
)


@pytest.mark.asyncio
async def test_create_llm_client_api_branch():
    client = await create_llm_client(LLMClientType.API)
    assert isinstance(client, ApiAnalyzerLLMClient)
    await client.aclose()


from api.services.blocks.analyzer.analyzer_llm_client import resolve_analyzer_api_key
from oddish.config import settings as _settings


class _RecordingAnthropic:
    last_api_key: object = "UNSET"

    def __init__(self, *a, api_key=None, **k):
        type(self).last_api_key = api_key
        self.messages = None

    async def close(self):
        pass


def _patch_anthropic(monkeypatch):
    _RecordingAnthropic.last_api_key = "UNSET"
    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _RecordingAnthropic,
    )


def test_resolve_analyzer_api_key_order(monkeypatch):
    monkeypatch.setattr(_settings, "analyzer_anthropic_api_key", "sk-analyzer")
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-global")
    assert resolve_analyzer_api_key("sk-explicit") == "sk-explicit"   # explicit wins
    assert resolve_analyzer_api_key(None) == "sk-analyzer"            # then analyzer key
    monkeypatch.setattr(_settings, "analyzer_anthropic_api_key", None)
    assert resolve_analyzer_api_key(None) == "sk-global"             # then global


def test_api_client_passes_explicit_api_key(monkeypatch):
    _patch_anthropic(monkeypatch)
    monkeypatch.setattr(_settings, "analyzer_anthropic_api_key", "sk-analyzer")
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-global")
    ApiAnalyzerLLMClient(api_key="sk-explicit")
    assert _RecordingAnthropic.last_api_key == "sk-explicit"


def test_api_client_uses_analyzer_key_when_no_explicit(monkeypatch):
    _patch_anthropic(monkeypatch)
    monkeypatch.setattr(_settings, "analyzer_anthropic_api_key", "sk-analyzer")
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-global")
    ApiAnalyzerLLMClient()
    assert _RecordingAnthropic.last_api_key == "sk-analyzer"


@pytest.mark.asyncio
async def test_create_llm_client_api_passes_api_key(monkeypatch):
    _patch_anthropic(monkeypatch)
    monkeypatch.setattr(_settings, "analyzer_anthropic_api_key", None)
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-global")
    await create_llm_client(LLMClientType.API, api_key="sk-passed")
    assert _RecordingAnthropic.last_api_key == "sk-passed"


@pytest.mark.asyncio
async def test_sandbox_client_streams_json_lines_and_closes():
    sent = {}

    class _FakeSandbox:
        id = "sbx-1"

    class _FakeRuntime:
        async def stream_chat(self, client, sandbox, *, content, claude_session_id,
                              daytona_session_id="cc", system_prompt=None):
            sent["content"] = content
            for d in [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]:
                yield d

    class _FakeDaytona:
        def __init__(self):
            self.deleted = False
        async def delete_sandbox(self, sandbox):
            self.deleted = True

    daytona = _FakeDaytona()
    client = SandboxAnalyzerLLMClient(
        sandbox=_FakeSandbox(),
        daytona_client=daytona,
        runtime=_FakeRuntime(),
        daytona_session_id="analyzer",
    )
    out = []
    async for chunk in client.stream("my prompt"):
        out.append(chunk)
    assert sent["content"] == "my prompt"
    assert [__import__("json").loads(c) for c in out] == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]
    await client.aclose()
    assert daytona.deleted is True


@pytest.mark.asyncio
async def test_fake_client_records_system_prompt():
    c = FakeAnalyzerLLMClient(chunks=["a"])
    chunks = [x async for x in c.stream("hi", system_prompt="be terse")]
    assert chunks == ["a"]
    assert c.last_system_prompt == "be terse"


from api.services.blocks.analyzer.analyzer_llm_client import ApiAnalyzerLLMClient


@pytest.mark.asyncio
async def test_create_llm_client_api_honors_model():
    c = await create_llm_client(LLMClientType.API, model="claude-haiku-4-5-20251001")
    assert isinstance(c, ApiAnalyzerLLMClient)
    assert c._model == "claude-haiku-4-5-20251001"
    await c.aclose()


@pytest.mark.asyncio
async def test_fake_client_download_file():
    c = FakeAnalyzerLLMClient(files={"out/reduce.json": b"{}"})
    assert await c._download_file("out/reduce.json") == b"{}"
    with pytest.raises(KeyError):
        await c._download_file("out/missing.jsonl")


def test_init_signature_has_no_duplicate_parameters():
    """Guards against a merge reintroducing a module-level SyntaxError.

    #810 and #812 each added ``thinking`` to this constructor in parallel. The
    parameter lists did not overlap textually, so git merged both cleanly into
    a duplicate argument -- a SyntaxError that made the whole module
    unimportable, taking every analyzer and report path down with it. Nothing
    in the suite caught it because an unimportable module fails at collection,
    which reads as an errored test file rather than a broken product.
    """
    import inspect

    names = list(inspect.signature(ApiAnalyzerLLMClient.__init__).parameters)
    assert len(names) == len(set(names)), f"duplicate parameter in {names}"


def test_thinking_defaults_to_disabled_and_is_overridable():
    # The dedupe had to pick one of two assignments; this pins the surviving
    # behaviour so a future cleanup cannot silently re-enable thinking, which
    # would spend the output budget on reasoning and truncate block JSON.
    assert ApiAnalyzerLLMClient(api_key="k")._thinking == {"type": "disabled"}
    explicit = {"type": "enabled", "budget_tokens": 1024}
    assert ApiAnalyzerLLMClient(api_key="k", thinking=explicit)._thinking == explicit
