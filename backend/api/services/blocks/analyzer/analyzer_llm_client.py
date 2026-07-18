from __future__ import annotations

import enum
import json
import os
from typing import AsyncIterator, Protocol, runtime_checkable

from anthropic import AsyncAnthropic

from oddish.config import settings
from oddish.db import generate_id
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient, RealDaytonaClient
from api.services.cc_chat.provisioner import Provisioner, delete_sandbox_quietly

_DEFAULT_MODEL = "claude-opus-4-8"


def resolve_analyzer_api_key(explicit: str | None = None) -> str | None:
    """Anthropic API key for analyzer blocks, most-specific first: an explicit
    per-block override, then the analyzer-specific key
    (``ANALYZER_ANTHROPIC_API_KEY``), then the global ``ANTHROPIC_API_KEY``.
    Returns None only when none are set (the SDK then errors on first call)."""
    return explicit or settings.analyzer_anthropic_api_key or settings.anthropic_api_key


class LLMClientType(str, enum.Enum):
    SANDBOX = "Sandbox"
    API = "Api"


@runtime_checkable
class AnalyzerLLMClient(Protocol):
    def stream(self, prompt: str) -> AsyncIterator[str]: ...
    async def aclose(self) -> None: ...


class FakeAnalyzerLLMClient:
    """Test double: yields canned chunks, or raises a configured exception."""

    def __init__(
        self,
        *,
        chunks: list[str] | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._chunks = chunks or []
        self._exc = exc

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk
        if self._exc is not None:
            raise self._exc

    async def aclose(self) -> None:
        return None


class ApiAnalyzerLLMClient:
    """Direct Anthropic API backend: streams text deltas for a single prompt."""

    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        max_tokens: int = 4096,
        api_key: str | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        key = resolve_analyzer_api_key(api_key)
        self._inner = AsyncAnthropic(api_key=key) if key else AsyncAnthropic()

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        async with self._inner.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def aclose(self) -> None:
        await self._inner.close()


_DAYTONA_SESSION_ID = "analyzer"
_AUTO_STOP_MINUTES = 15
_AUTO_DELETE_MINUTES = 30


class SandboxAnalyzerLLMClient:
    """Daytona-sandbox backend: runs claude-code and yields one JSON string per
    stream-json event. Provisioning happens in ``create_llm_client`` (an async
    factory) -- constructors cannot be awaited."""

    def __init__(
        self,
        *,
        sandbox: CreatedSandbox,
        daytona_client: DaytonaClient,
        runtime: ClaudeCodeRuntime,
        daytona_session_id: str = _DAYTONA_SESSION_ID,
    ) -> None:
        self._sandbox = sandbox
        self._client = daytona_client
        self._runtime = runtime
        self._session_id = daytona_session_id

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        async for event in self._runtime.stream_chat(
            self._client,
            self._sandbox,
            content=prompt,
            claude_session_id=None,
            daytona_session_id=self._session_id,
        ):
            yield json.dumps(event)

    async def aclose(self) -> None:
        await delete_sandbox_quietly(self._client, self._sandbox)


async def create_llm_client(
    llm_client_type: LLMClientType, *, api_key: str | None = None
) -> AnalyzerLLMClient:
    if llm_client_type == LLMClientType.API:
        return ApiAnalyzerLLMClient(api_key=api_key)

    if llm_client_type == LLMClientType.SANDBOX:
        daytona_client = RealDaytonaClient(api_key=os.environ["DAYTONA_API_KEY"])
        sandbox = await Provisioner(client=daytona_client).create(
            env_vars={"ANTHROPIC_API_KEY": resolve_analyzer_api_key(api_key) or ""},
            auto_stop_minutes=_AUTO_STOP_MINUTES,
            auto_delete_minutes=_AUTO_DELETE_MINUTES,
            labels={"app": "analyzer", "session_id": generate_id()},
            daytona_session_id=_DAYTONA_SESSION_ID,
        )
        runtime = ClaudeCodeRuntime()
        await runtime.install(daytona_client, sandbox)
        return SandboxAnalyzerLLMClient(
            sandbox=sandbox, daytona_client=daytona_client, runtime=runtime
        )

    raise ValueError(f"unknown llm_client_type: {llm_client_type!r}")
