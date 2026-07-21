from __future__ import annotations

import enum
import warnings
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from oddish.config import OPENAI_PROVIDER_OPENAI, settings

_DEFAULT_MODEL = "claude-opus-4-8"

# Analyzer blocks ask for a single JSON object and size max_tokens for that
# object alone. Thinking is billed against the same max_tokens ceiling, so on a
# model that thinks by default (sonnet-5 and later: omitting `thinking` runs
# adaptive, where sonnet-4-6 ran it off) reasoning silently eats the budget and
# the JSON is cut mid-token. Pin it off rather than relying on the model default.
_THINKING_DISABLED: dict = {"type": "disabled"}


class OutputBudgetExceeded(RuntimeError):
    """The model hit ``max_tokens`` before finishing its response.

    Raised instead of letting a truncated body reach the block parser, where it
    only ever surfaced as an unexplained ``non-JSON output`` error.
    """


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
        max_tokens: int | None = None,
        thinking: dict | None = None,
        api_key: str | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens if max_tokens is not None else 4096
        self._thinking = thinking if thinking is not None else _THINKING_DISABLED
        key = resolve_analyzer_api_key(api_key)
        self._inner = AsyncAnthropic(api_key=key) if key else AsyncAnthropic()

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            thinking=self._thinking,
            messages=[{"role": "user", "content": prompt}],
        )
        if system_prompt is not None:
            kwargs["system"] = system_prompt
        async with self._inner.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        # text_stream ends identically on a complete reply and on a truncated
        # one, so without this the caller parses a half-written object.
        if getattr(final, "stop_reason", None) == "max_tokens":
            raise OutputBudgetExceeded(
                f"model {self._model} hit max_tokens={self._max_tokens} before "
                f"finishing (thinking={self._thinking.get('type')!r}); "
                f"usage={getattr(final, 'usage', None)}"
            )

    async def aclose(self) -> None:
        await self._inner.close()


def _build_openai_client(
    *, model: str, api_key: str | None = None
) -> tuple[AsyncOpenAI, str]:
    """Resolve public-OpenAI vs Azure and return (client, runtime model id).
    A module-level seam: tests patch this instead of the class, so construction
    never needs live credentials."""
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
            # ApiAnalyzerLLMClient) but maps to the wire param OpenAI accepts.
            kwargs["max_completion_tokens"] = self._max_tokens

        async with self._client.chat.completions.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content.delta":
                    yield event.delta

    async def aclose(self) -> None:
        await self._client.close()


SandboxClientFactory = Callable[..., Awaitable[AnalyzerLLMClient]]

_sandbox_client_factory: SandboxClientFactory | None = None


def register_sandbox_client_factory(factory: SandboxClientFactory) -> None:
    """Install the Daytona-sandbox backend from the hosted layer.

    The sandbox client needs cc_chat's provisioner, which core must not import;
    ``backend.api.services.blocks.analyzer.sandbox_llm_client`` registers itself
    here on import. Unregistered, only the SANDBOX branch is unavailable."""
    global _sandbox_client_factory
    _sandbox_client_factory = factory


async def create_llm_client(
    llm_client_type: LLMClientType,
    *,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    response_format: Any | None = None,
) -> AnalyzerLLMClient:
    if llm_client_type == LLMClientType.API:
        return ApiAnalyzerLLMClient(
            model=model or _DEFAULT_MODEL, max_tokens=max_tokens, api_key=api_key
        )

    if llm_client_type == LLMClientType.OPENAI:
        if not model:
            raise ValueError(
                "OPENAI llm_client_type requires an explicit model= "
                "(no verdict-specific default is applied here)"
            )
        return OpenAIAnalyzerLLMClient(
            model=model,
            max_tokens=max_tokens,
            response_format=response_format,
            api_key=api_key,
        )

    if llm_client_type == LLMClientType.SANDBOX:
        if _sandbox_client_factory is None:
            raise RuntimeError(
                "SANDBOX llm_client_type needs the hosted sandbox backend; import "
                "api.services.blocks.analyzer.sandbox_llm_client to register it"
            )
        return await _sandbox_client_factory(model=model, api_key=api_key)

    raise ValueError(f"unknown llm_client_type: {llm_client_type!r}")
