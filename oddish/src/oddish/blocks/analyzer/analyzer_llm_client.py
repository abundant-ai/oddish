from __future__ import annotations

import enum
import warnings
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from oddish.analyze.analysis_cost import AnalysisUsage, usage_from_api_message
from oddish.blocks.analyzer.claude_cli_client import ClaudeCliClient, CliConfig
from oddish.config import OPENAI_PROVIDER_OPENAI, _infer_provider_prefix, settings

_DEFAULT_MODEL = "claude-opus-4-8"

# Providers reached through the OpenAI SDK (public OpenAI and Azure both are).
# Any other provider -- or an unclassifiable model id -- routes to Anthropic,
# which is the analyzer default and the safe fallback.
_OPENAI_SDK_PROVIDERS = {"openai", "azure", "azure_openai"}


def _model_uses_openai_sdk(model: str) -> bool:
    return (_infer_provider_prefix(model) or "") in _OPENAI_SDK_PROVIDERS


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
    # Filesystem-aware Claude Code process running in the worker that already
    # downloaded the task and trial artifacts.
    CLAUDE_CLI = "ClaudeCli"
    # Direct provider API. Speaks Anthropic or OpenAI/Azure, chosen from the
    # model id -- there is no separate OpenAI backend to select.
    API = "Api"


@dataclass(frozen=True)
class SandboxConfig:
    """Hosted sandbox capabilities requested by an AnalyzerBlock.

    Core treats this as declarative data. The registered hosted factory owns
    credential minting, CLI/runtime installation, and cleanup.
    """

    install_oddish_cli: bool = False
    oddish_org_id: str | None = None
    oddish_api_base_url: str | None = None
    oddish_api_scope: str = "read"
    oddish_api_key: str | None = None
    reasoning_effort: str | None = None
    trajectory_tail_bytes: int | None = None
    # Serialized JSON Schema handed to claude-code's ``--json-schema``, which
    # constrains generation and surfaces the object as ``structured_output`` on
    # the final stream-json event. None leaves the run unconstrained.
    json_schema: str | None = None
    session_id: str = "analyzer"
    labels: dict[str, str] = field(default_factory=dict)
    files_to_upload: dict[str, bytes] = field(default_factory=dict)
    setup_commands: tuple[str, ...] = ()
    auto_stop_minutes: int | None = None
    auto_delete_minutes: int | None = None
    snapshot: str | None = None


@runtime_checkable
class AnalyzerLLMClient(Protocol):
    # Usage for the most recent stream(), or None when the backend reports none.
    # Read by AnalyzerBlock after the stream drains, so it must survive the call.
    last_usage: AnalysisUsage | None

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
        last_usage: AnalysisUsage | None = None,
    ) -> None:
        self._chunks = chunks or []
        self._exc = exc
        self._files = files or {}
        self.last_system_prompt: str | None = None
        self.last_usage = last_usage

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


class ApiAnalyzerLLMClient:
    """Direct provider-API backend: streams a single prompt's output as text.

    One client for both Anthropic and OpenAI/Azure -- the provider is chosen
    from the model id (``_model_uses_openai_sdk``), so callers select the
    single API backend and pass a model rather than picking a provider-specific
    client. ``thinking`` / ``output_schema`` apply only on the Anthropic path;
    ``response_format`` only on the OpenAI path; the rest are shared."""

    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        api_key: str | None = None,
        output_schema: dict | None = None,
        response_format: Any | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._uses_openai = _model_uses_openai_sdk(model)
        self.last_usage: AnalysisUsage | None = None

        if self._uses_openai:
            self._response_format = response_format
            self._openai, self._model = _build_openai_client(
                model=model, api_key=api_key
            )
            self._anthropic = None
        else:
            # Thinking is set EXPLICITLY, never left to the model default.
            # Sonnet 5 runs adaptive thinking when the field is omitted (Sonnet
            # 4.6 ran without it), and max_tokens caps thinking + response
            # *together* -- so moving analysis_model to sonnet-5 silently spent
            # the output budget on reasoning and truncated block output
            # mid-JSON. Analyzer blocks parse their output, so a truncated
            # response is a hard failure, not a degraded one. Pass thinking= to
            # opt back in per call site.
            self._thinking = thinking if thinking is not None else _THINKING_DISABLED
            # When set, the response is constrained to this JSON schema during
            # generation instead of being hand-written into free text.
            self._output_schema = output_schema
            key = resolve_analyzer_api_key(api_key)
            self._anthropic = AsyncAnthropic(api_key=key) if key else AsyncAnthropic()
            self._openai = None

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        self.last_usage = None
        if self._uses_openai:
            async for text in self._stream_openai(prompt, system_prompt):
                yield text
        else:
            async for text in self._stream_anthropic(prompt, system_prompt):
                yield text

    async def _stream_anthropic(
        self, prompt: str, system_prompt: str | None
    ) -> AsyncIterator[str]:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens if self._max_tokens is not None else 4096,
            thinking=self._thinking,
            messages=[{"role": "user", "content": prompt}],
        )
        if system_prompt is not None:
            kwargs["system"] = system_prompt
        if self._output_schema is not None:
            # output_config is not a named kwarg on anthropic 0.76.0 (the break
            # behind #493), so it goes through extra_body.
            kwargs["extra_body"] = {
                "output_config": {
                    "format": {"type": "json_schema", "schema": self._output_schema}
                }
            }
        async with self._anthropic.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        # Stashed before the truncation check below: a run that blew its budget
        # still spent the tokens, and dropping it would under-report that spend.
        self.last_usage = usage_from_api_message(
            getattr(final, "usage", None), self._model
        )
        # text_stream ends identically on a complete reply and on a truncated
        # one, so without this the caller parses a half-written object.
        if getattr(final, "stop_reason", None) == "max_tokens":
            raise OutputBudgetExceeded(
                f"model {self._model} hit max_tokens={self._max_tokens} before "
                f"finishing (thinking={self._thinking.get('type')!r}); "
                f"usage={getattr(final, 'usage', None)}"
            )

    async def _stream_openai(
        self, prompt: str, system_prompt: str | None
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
            # API; the constructor name stays `max_tokens` (uniform across
            # providers) but maps to the wire param OpenAI accepts.
            kwargs["max_completion_tokens"] = self._max_tokens

        async with self._openai.chat.completions.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content.delta":
                    yield event.delta
        # OpenAI streaming chunks carry no usage, so self.last_usage stays None:
        # OpenAI spend is not folded into analysis_costs the way the Anthropic
        # path's usage_from_api_message does it.

    async def aclose(self) -> None:
        if self._uses_openai:
            await self._openai.close()
        else:
            await self._anthropic.close()


SandboxClientFactory = Callable[..., Awaitable[AnalyzerLLMClient]]

_sandbox_client_factory: SandboxClientFactory | None = None


def register_sandbox_client_factory(factory: SandboxClientFactory) -> None:
    """Install the Daytona-sandbox backend from the hosted layer.

    The sandbox client needs cc_chat's provisioner, which core must not import;
    ``backend.api.services.blocks.analyzer.sandbox_llm_client`` registers itself
    here on import. Unregistered, only the SANDBOX branch is unavailable."""
    global _sandbox_client_factory
    _sandbox_client_factory = factory


def sandbox_client_factory_registered() -> bool:
    """Whether the hosted layer installed its sandbox provisioner."""
    return _sandbox_client_factory is not None


async def create_llm_client(
    llm_client_type: LLMClientType,
    *,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    response_format: Any | None = None,
    sandbox_config: SandboxConfig | None = None,
    cli_config: CliConfig | None = None,
) -> AnalyzerLLMClient:
    if llm_client_type == LLMClientType.API:
        if sandbox_config:
            raise ValueError("sandbox_config is only supported by the sandbox backend")
        return ApiAnalyzerLLMClient(
            model=model or _DEFAULT_MODEL,
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
        config = sandbox_config or SandboxConfig()
        return await _sandbox_client_factory(
            model=model, api_key=api_key, sandbox_config=config
        )

    if llm_client_type == LLMClientType.CLAUDE_CLI:
        if cli_config is None:
            raise ValueError(
                "CLAUDE_CLI needs a cli_config naming the directories it may read"
            )
        return ClaudeCliClient(model=model, config=cli_config)

    raise ValueError(f"unknown llm_client_type: {llm_client_type!r}")
