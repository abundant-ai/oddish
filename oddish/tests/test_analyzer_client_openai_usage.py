"""The OpenAI path must report its spend the way the Anthropic path does.

Task-verdict synthesis is the only OpenAI caller, and it is an AnalyzerBlock
whose record_cost bails on ``usage is None`` -- so without this the entire
``task_verdict`` cost bucket is silently absent from analysis_costs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oddish.blocks.analyzer import analyzer_llm_client as mod

MODEL = "gpt-5.4"


class _FakeStream:
    def __init__(self, usage):
        self._usage = usage

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            yield SimpleNamespace(type="content.delta", delta="hello")
            yield SimpleNamespace(type="content.done")

        return gen()

    @property
    def current_completion_snapshot(self):
        # The real SDK folds the usage chunk onto this snapshot as the stream
        # drains; the client reads spend from here so it survives even when
        # get_final_completion() raises on a truncated finish.
        return SimpleNamespace(usage=self._usage)

    async def get_final_completion(self):
        return SimpleNamespace(usage=self._usage)


class _FakeCompletions:
    def __init__(self, usage):
        self._usage = usage
        self.captured: dict = {}

    def stream(self, **kwargs):
        self.captured = kwargs
        return _FakeStream(self._usage)


class _FakeOpenAI:
    def __init__(self, usage):
        self.chat = SimpleNamespace(completions=_FakeCompletions(usage))

    async def close(self):
        return None


def _client(monkeypatch, usage, *, deployment=None):
    fake = _FakeOpenAI(usage)
    # On Azure the second element is the deployment id, which diverges from the
    # canonical model; default it equal to model to mirror the public-OpenAI path.
    wire = deployment if deployment is not None else MODEL
    monkeypatch.setattr(
        mod, "_build_openai_client", lambda *, model, api_key: (fake, wire)
    )
    return mod.ApiAnalyzerLLMClient(model=MODEL, max_tokens=256), fake


def _usage(cached=400):
    return SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


@pytest.mark.asyncio
async def test_openai_stream_records_usage(monkeypatch):
    client, _ = _client(monkeypatch, _usage())

    chunks = [c async for c in client.stream("prompt")]

    assert chunks == ["hello"]
    assert client.last_usage is not None, "OpenAI spend was not captured"
    assert client.last_usage.input_tokens == 1000
    assert client.last_usage.output_tokens == 200
    assert client.last_usage.cache_read_tokens == 400
    assert client.last_usage.cost_usd > 0


@pytest.mark.asyncio
async def test_openai_stream_asks_the_api_for_usage(monkeypatch):
    """Usage only arrives on the wire when include_usage is requested."""
    client, fake = _client(monkeypatch, _usage())

    [c async for c in client.stream("prompt")]

    assert fake.chat.completions.captured.get("stream_options") == {
        "include_usage": True
    }


@pytest.mark.asyncio
async def test_azure_prices_canonical_model_not_deployment(monkeypatch):
    """Azure's deployment id is unpriceable; usage must price the model id.

    On the Azure path _build_openai_client returns the deployment id as the
    wire model. Pricing off that name leaves task_verdict rows with tokens but
    a null cost_usd -- the deployment must be sent on the request while the
    canonical model is what gets priced.
    """
    client, fake = _client(monkeypatch, _usage(), deployment="azure-gpt-5-4")

    [c async for c in client.stream("prompt")]

    assert fake.chat.completions.captured.get("model") == "azure-gpt-5-4"
    assert client.last_usage is not None
    assert client.last_usage.model == MODEL
    assert client.last_usage.cost_usd > 0


@pytest.mark.asyncio
async def test_missing_usage_leaves_last_usage_none(monkeypatch):
    """A provider that reports nothing must not fabricate a cost row."""
    client, _ = _client(monkeypatch, None)

    [c async for c in client.stream("prompt")]

    assert client.last_usage is None


class _RaisingFinalStream(_FakeStream):
    """A length/content-filter finish makes the real get_final_completion()
    raise while re-parsing the response_format, after the usage chunk was
    already accumulated onto the snapshot."""

    async def get_final_completion(self):
        raise RuntimeError("length finish -- response_format validation failed")


@pytest.mark.asyncio
async def test_usage_recorded_even_when_finalization_raises(monkeypatch):
    """The spend must land in last_usage before the truncation raise escapes;
    otherwise those tokens vanish from analysis_costs exactly like the bug this
    PR set out to fix."""
    fake = _FakeOpenAI(_usage())
    monkeypatch.setattr(fake.chat.completions, "stream", lambda **kw: _RaisingFinalStream(_usage()))
    monkeypatch.setattr(
        mod, "_build_openai_client", lambda *, model, api_key: (fake, MODEL)
    )
    client = mod.ApiAnalyzerLLMClient(model=MODEL, max_tokens=256)

    with pytest.raises(RuntimeError):
        [c async for c in client.stream("prompt")]

    assert client.last_usage is not None, "spend dropped when finalization raised"
    assert client.last_usage.input_tokens == 1000
    assert client.last_usage.output_tokens == 200
    assert client.last_usage.cost_usd > 0


def test_explicit_openai_prefix_strips_wire_model_and_stays_quiet(monkeypatch):
    import warnings as _warnings

    from oddish.blocks.analyzer import analyzer_llm_client as mod
    from oddish.config import settings

    monkeypatch.setattr(settings, "openai_provider", "azure")
    monkeypatch.setattr(settings, "openai_api_key", "sk-public")

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")  # any warning fails the test
        _, wire = mod._build_openai_client(model="openai/gpt-5.2")
    assert wire == "gpt-5.2"


def test_bare_id_on_public_default_still_warns(monkeypatch):
    import pytest as _pytest

    from oddish.blocks.analyzer import analyzer_llm_client as mod
    from oddish.config import settings

    monkeypatch.setattr(settings, "openai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-public")

    with _pytest.warns(UserWarning, match="public OpenAI"):
        _, wire = mod._build_openai_client(model="gpt-5.2")
    assert wire == "gpt-5.2"

    # An explicit prefix stays quiet even when the default is also public.
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        _, wire = mod._build_openai_client(model="openai/gpt-5.2")
    assert wire == "gpt-5.2"


def test_anthropic_path_normalizes_wire_model():
    from oddish.blocks.analyzer.analyzer_llm_client import ApiAnalyzerLLMClient

    # The Anthropic SDK only accepts plain API ids: the transport prefix and
    # Bedrock-shaped ids both normalize at construction.
    client = ApiAnalyzerLLMClient(model="anthropic/claude-haiku-4-5")
    assert client._model == "claude-haiku-4-5"

    client = ApiAnalyzerLLMClient(
        model="global.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    assert client._model == "claude-haiku-4-5"
