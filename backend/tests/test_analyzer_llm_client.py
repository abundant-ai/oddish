import pytest

from api.services.analyzer_llm_client import (
    LLMClientType,
    FakeAnalyzerLLMClient,
    ApiAnalyzerLLMClient,
)


async def _collect(client, prompt):
    out = []
    async for chunk in client.stream(prompt):
        out.append(chunk)
    return out


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
    class _AStream:
        def __init__(self, parts):
            self._parts = parts
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
        "api.services.analyzer_llm_client.AsyncAnthropic", _FakeAnthropic
    )
    client = ApiAnalyzerLLMClient()
    assert await _collect(client, "hi") == ["Hel", "lo"]
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
        "api.services.analyzer_llm_client.AsyncAnthropic", _FakeAnthropic
    )
    client = ApiAnalyzerLLMClient()
    await client.aclose()
    assert closed["n"] == 1


from api.services.analyzer_llm_client import (
    SandboxAnalyzerLLMClient,
    create_llm_client,
)


@pytest.mark.asyncio
async def test_create_llm_client_api_branch():
    client = await create_llm_client(LLMClientType.API)
    assert isinstance(client, ApiAnalyzerLLMClient)
    await client.aclose()


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
