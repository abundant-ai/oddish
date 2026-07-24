import warnings
from types import SimpleNamespace

import pytest

from oddish.blocks.analyzer.analyzer_llm_client import (
    LLMClientType,
    FakeAnalyzerLLMClient,
    ApiAnalyzerLLMClient,
    _build_openai_client,
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
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _FakeAnthropic,
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
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
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
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
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
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
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
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
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
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _FakeAnthropic,
    )
    client = ApiAnalyzerLLMClient()
    await client.aclose()
    assert closed["n"] == 1


from api.services.blocks.analyzer.sandbox_llm_client import SandboxAnalyzerLLMClient
from oddish.blocks.analyzer.analyzer_llm_client import create_llm_client


@pytest.mark.asyncio
async def test_create_llm_client_api_branch():
    client = await create_llm_client(LLMClientType.API)
    assert isinstance(client, ApiAnalyzerLLMClient)
    await client.aclose()


from oddish.blocks.analyzer.analyzer_llm_client import resolve_analyzer_api_key
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
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _RecordingAnthropic,
    )


def test_resolve_analyzer_api_key_order(monkeypatch):
    monkeypatch.setattr(_settings, "analyzer_anthropic_api_key", "sk-analyzer")
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-global")
    assert resolve_analyzer_api_key("sk-explicit") == "sk-explicit"  # explicit wins
    assert resolve_analyzer_api_key(None) == "sk-analyzer"  # then analyzer key
    monkeypatch.setattr(_settings, "analyzer_anthropic_api_key", None)
    assert resolve_analyzer_api_key(None) == "sk-global"  # then global


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
        async def stream_chat(
            self,
            client,
            sandbox,
            *,
            content,
            claude_session_id,
            daytona_session_id="cc",
            system_prompt=None,
            json_schema=None,
        ):
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


from oddish.blocks.analyzer.analyzer_llm_client import ApiAnalyzerLLMClient


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


# --- OpenAI/Azure path of the direct-API client -----------------------------
# ApiAnalyzerLLMClient routes an OpenAI-family model (gpt-*) through the OpenAI
# SDK; there is no separate OpenAI client class to construct.


class _FakeStreamEvent:
    def __init__(self, type_, delta=None):
        self.type = type_
        self.delta = delta


class _FakeOpenAIStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def __aiter__(self):
        for event in self._events:
            yield event


class _FakeOpenAICompletions:
    def __init__(self, events, sent):
        self._events = events
        self._sent = sent

    def stream(self, **kwargs):
        self._sent.update(kwargs)
        return _FakeOpenAIStream(self._events)


class _FakeOpenAIChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeAsyncOpenAI:
    def __init__(self, events, sent):
        self.chat = _FakeOpenAIChat(_FakeOpenAICompletions(events, sent))
        self.closed = False

    async def close(self):
        self.closed = True


def _patch_openai_builder(monkeypatch, events, *, runtime_model="gpt-5.4"):
    """Bypass ``_build_openai_client`` so constructing the client never needs
    live OpenAI/Azure credentials."""
    sent: dict = {}
    fake = _FakeAsyncOpenAI(events, sent)

    def _builder(*, model, api_key=None):
        return fake, runtime_model

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_llm_client._build_openai_client",
        _builder,
    )
    return fake, sent


@pytest.mark.asyncio
async def test_openai_client_yields_only_content_deltas(monkeypatch):
    events = [
        _FakeStreamEvent("chunk"),
        _FakeStreamEvent("content.delta", delta="Hel"),
        _FakeStreamEvent("logprobs.content.delta", delta="ignored"),
        _FakeStreamEvent("content.delta", delta="lo"),
    ]
    fake, _sent = _patch_openai_builder(monkeypatch, events)
    client = ApiAnalyzerLLMClient(model="gpt-5.4")
    assert await _collect(client, "hi") == ["Hel", "lo"]
    await client.aclose()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_openai_client_forwards_response_format_when_set(monkeypatch):
    events = [_FakeStreamEvent("content.delta", delta="x")]
    fake, sent = _patch_openai_builder(monkeypatch, events)

    class _Fmt:
        pass

    client = ApiAnalyzerLLMClient(model="gpt-5.4", response_format=_Fmt)
    await _collect(client, "hi")
    assert sent["response_format"] is _Fmt


@pytest.mark.asyncio
async def test_openai_client_omits_response_format_when_unset(monkeypatch):
    events = [_FakeStreamEvent("content.delta", delta="x")]
    fake, sent = _patch_openai_builder(monkeypatch, events)
    client = ApiAnalyzerLLMClient(model="gpt-5.4")
    await _collect(client, "hi")
    assert "response_format" not in sent


@pytest.mark.asyncio
async def test_openai_client_forwards_max_tokens_as_max_completion_tokens(monkeypatch):
    """gpt-5.x-class models reject the legacy `max_tokens` wire param; the
    constructor keeps the `max_tokens` name, but on the OpenAI path it must be
    sent as `max_completion_tokens`."""
    events = [_FakeStreamEvent("content.delta", delta="x")]
    fake, sent = _patch_openai_builder(monkeypatch, events)
    client = ApiAnalyzerLLMClient(model="gpt-5.4", max_tokens=4096)
    await _collect(client, "hi")
    assert sent["max_completion_tokens"] == 4096
    assert "max_tokens" not in sent


@pytest.mark.asyncio
async def test_openai_client_omits_max_completion_tokens_when_unset(monkeypatch):
    events = [_FakeStreamEvent("content.delta", delta="x")]
    fake, sent = _patch_openai_builder(monkeypatch, events)
    client = ApiAnalyzerLLMClient(model="gpt-5.4")
    await _collect(client, "hi")
    assert "max_completion_tokens" not in sent
    assert "max_tokens" not in sent


@pytest.mark.asyncio
async def test_openai_client_sends_system_prompt_as_system_message(monkeypatch):
    events = [_FakeStreamEvent("content.delta", delta="x")]
    fake, sent = _patch_openai_builder(monkeypatch, events)
    client = ApiAnalyzerLLMClient(model="gpt-5.4")
    [chunk async for chunk in client.stream("hi", system_prompt="be terse")]
    assert sent["messages"][0] == {"role": "system", "content": "be terse"}
    assert sent["messages"][-1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_openai_client_omits_system_message_when_unset(monkeypatch):
    events = [_FakeStreamEvent("content.delta", delta="x")]
    fake, sent = _patch_openai_builder(monkeypatch, events)
    client = ApiAnalyzerLLMClient(model="gpt-5.4")
    await _collect(client, "hi")
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


def test_build_openai_client_warns_and_uses_public_key(monkeypatch):
    monkeypatch.setattr(_settings, "openai_provider", "openai")
    monkeypatch.setattr(_settings, "openai_api_key", "sk-public")
    captured = {}

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key=None, base_url=None):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncOpenAI",
        _FakeAsyncOpenAI,
    )
    with pytest.warns(UserWarning, match="public OpenAI API"):
        client, runtime_model = _build_openai_client(model="gpt-5.4")
    assert captured["api_key"] == "sk-public"
    assert captured["base_url"] is None
    assert runtime_model == "gpt-5.4"


def test_build_openai_client_azure_resolves_deployment_and_does_not_warn(
    monkeypatch,
):
    monkeypatch.setattr(_settings, "openai_provider", "azure")
    monkeypatch.setattr(_settings, "azure_openai_api_key", "sk-azure")
    monkeypatch.setattr(
        _settings, "azure_openai_endpoint", "https://example.openai.azure.com/openai/v1"
    )
    monkeypatch.setattr(_settings, "azure_openai_api_version", "2024-05-01")
    monkeypatch.setattr(
        _settings, "azure_openai_deployments", {"gpt-5.4": "prod-gpt-5-4"}
    )
    captured = {}

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key=None, base_url=None):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncOpenAI",
        _FakeAsyncOpenAI,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client, runtime_model = _build_openai_client(model="gpt-5.4")
    assert runtime_model == "prod-gpt-5-4"
    assert captured["api_key"] == "sk-azure"
    assert captured["base_url"] == "https://example.openai.azure.com/openai/v1"


@pytest.mark.asyncio
async def test_create_llm_client_api_routes_openai_model_through_openai_sdk(
    monkeypatch,
):
    """An OpenAI-family model reaches the API backend, which routes it through
    the OpenAI SDK -- no separate OPENAI client type to select."""
    events = [_FakeStreamEvent("content.delta", delta="x")]
    fake, _sent = _patch_openai_builder(monkeypatch, events)
    client = await create_llm_client(LLMClientType.API, model="gpt-5.4")
    assert isinstance(client, ApiAnalyzerLLMClient)
    assert client._uses_openai is True
    await client.aclose()


def test_llm_client_type_values_are_pinned():
    """The enum's string values land in analyzer_blocks.llm_client_type
    (analyzer_block.py persists ``.value``), so a typo would be written to rows
    and silently break later queries. Comparisons elsewhere are by identity, so
    nothing else would catch it."""
    assert LLMClientType.API.value == "Api"
    assert LLMClientType.SANDBOX.value == "Sandbox"


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


# --- usage capture -------------------------------------------------------


class _UsageStream(_AStream):
    """A stream whose final message carries real token counts."""

    def __init__(self, parts, stop_reason="end_turn", usage=None):
        super().__init__(parts, stop_reason)
        self._usage = usage

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self._stop_reason, usage=self._usage)


_USAGE = SimpleNamespace(
    input_tokens=1000,
    output_tokens=200,
    cache_read_input_tokens=500,
    cache_creation_input_tokens=100,
)


@pytest.mark.asyncio
async def test_stream_captures_usage(monkeypatch):
    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _fake_anthropic(lambda: _UsageStream(["hi"], usage=_USAGE)),
    )
    c = ApiAnalyzerLLMClient(model="claude-opus-4-8", api_key="k")
    await _collect(c, "p")
    assert c.last_usage is not None
    assert c.last_usage.input_tokens == 1600
    assert c.last_usage.output_tokens == 200
    assert c.last_usage.source == "estimated"
    assert c.last_usage.cost_usd > 0


@pytest.mark.asyncio
async def test_usage_is_none_when_api_reports_none(monkeypatch):
    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _fake_anthropic(lambda: _UsageStream(["hi"], usage=None)),
    )
    c = ApiAnalyzerLLMClient(api_key="k")
    await _collect(c, "p")
    assert c.last_usage is None


@pytest.mark.asyncio
async def test_truncated_run_still_reports_its_spend(monkeypatch):
    """A block that blew max_tokens burned those tokens; the raise must not
    discard the usage."""
    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _fake_anthropic(
            lambda: _UsageStream(["hi"], stop_reason="max_tokens", usage=_USAGE)
        ),
    )
    c = ApiAnalyzerLLMClient(model="claude-opus-4-8", api_key="k")
    with pytest.raises(OutputBudgetExceeded):
        await _collect(c, "p")
    assert c.last_usage is not None
    assert c.last_usage.input_tokens == 1600
