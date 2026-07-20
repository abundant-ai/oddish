from __future__ import annotations

import enum
import json
import os
import warnings
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from oddish.config import OPENAI_PROVIDER_OPENAI, settings
from oddish.db import generate_id
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import (
    CreatedSandbox,
    DaytonaClient,
    RealDaytonaClient,
)
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
    OPENAI = "OpenAi"


@runtime_checkable
class AnalyzerLLMClient(Protocol):
    def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]: ...
    async def aclose(self) -> None: ...


class FakeAnalyzerLLMClient:
    """Test double: yields canned chunks, or raises a configured exception."""

    def __init__(
        self,
        *,
        chunks: list[str] | None = None,
        exc: BaseException | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self._chunks = chunks or []
        self._exc = exc
        self._files = files or {}
        self.last_system_prompt: str | None = None

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        self.last_system_prompt = system_prompt
        for chunk in self._chunks:
            yield chunk
        if self._exc is not None:
            raise self._exc

    async def _download_file(self, path: str) -> bytes:
        return self._files[path]

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

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if system_prompt is not None:
            kwargs["system"] = system_prompt
        async with self._inner.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text

    async def aclose(self) -> None:
        await self._inner.close()


def _build_openai_client(
    *, model: str, api_key: str | None = None
) -> tuple[AsyncOpenAI, str]:
    """Resolve public-OpenAI vs Azure exactly as the sync
    ``_build_verdict_openai_client`` in ``oddish.analyze.classifier`` does, so
    both verdict paths reach the identical deployment. A module-level seam:
    tests patch this instead of the class, so construction never needs live
    credentials."""
    provider = settings.get_openai_provider()
    if provider == OPENAI_PROVIDER_OPENAI:
        warnings.warn(settings.get_public_openai_warning(), stacklevel=2)
        public = settings.require_public_openai_config(api_key=api_key)
        return AsyncOpenAI(api_key=public["api_key"]), model

    azure = settings.require_azure_openai_config()
    deployment = settings.resolve_azure_openai_deployment(model)
    return (
        AsyncOpenAI(
            api_key=azure["api_key"],
            base_url=settings.get_azure_openai_base_url(),
        ),
        deployment,
    )


class OpenAIAnalyzerLLMClient:
    """OpenAI/Azure backend: streams content deltas for a single prompt via
    ``chat.completions.stream``."""

    def __init__(
        self,
        *,
        model: str,
        max_tokens: int | None = None,
        response_format: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self._max_tokens = max_tokens
        self._response_format = response_format
        self._client, self._model = _build_openai_client(model=model, api_key=api_key)

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        messages: list[dict] = []
        if system_prompt is not None:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict = dict(model=self._model, messages=messages)
        if self._response_format is not None:
            kwargs["response_format"] = self._response_format
        if self._max_tokens is not None:
            # gpt-5.x-class models reject the legacy `max_tokens` param at the
            # API; `_max_tokens` stays the constructor's name (uniform with
            # ApiAnalyzerLLMClient) but maps to the wire param OpenAI actually
            # accepts, matching classifier.py's `_compute_task_verdict_openai`.
            kwargs["max_completion_tokens"] = self._max_tokens

        async with self._client.chat.completions.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content.delta":
                    yield event.delta

    async def aclose(self) -> None:
        await self._client.close()


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

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        async for event in self._runtime.stream_chat(
            self._client,
            self._sandbox,
            content=prompt,
            claude_session_id=None,
            daytona_session_id=self._session_id,
            system_prompt=system_prompt,
        ):
            yield json.dumps(event)

    async def _download_file(self, path: str) -> bytes:
        return await self._client.download_file(self._sandbox, src_path=path)

    async def aclose(self) -> None:
        await delete_sandbox_quietly(self._client, self._sandbox)


async def create_llm_client(
    llm_client_type: LLMClientType,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> AnalyzerLLMClient:
    if llm_client_type == LLMClientType.API:
        return ApiAnalyzerLLMClient(model=model or _DEFAULT_MODEL, api_key=api_key)

    if llm_client_type == LLMClientType.OPENAI:
        if not model:
            raise ValueError(
                "OPENAI llm_client_type requires an explicit model= "
                "(no verdict-specific default is applied here)"
            )
        return OpenAIAnalyzerLLMClient(model=model, api_key=api_key)

    if llm_client_type == LLMClientType.SANDBOX:
        daytona_client = RealDaytonaClient(api_key=os.environ["DAYTONA_API_KEY"])
        env_vars = {"ANTHROPIC_API_KEY": resolve_analyzer_api_key(api_key) or ""}
        if model:
            env_vars["ANTHROPIC_MODEL"] = model
        sandbox = await Provisioner(client=daytona_client).create(
            env_vars=env_vars,
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
