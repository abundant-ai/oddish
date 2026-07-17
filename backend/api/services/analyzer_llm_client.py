from __future__ import annotations

import enum
import logging
from typing import AsyncIterator, Protocol, runtime_checkable

from anthropic import AsyncAnthropic

log = logging.getLogger("oddish.analyzer_block.client")

_DEFAULT_MODEL = "claude-opus-4-8"


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

    def __init__(self, *, model: str = _DEFAULT_MODEL, max_tokens: int = 4096) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._inner = AsyncAnthropic()

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        async with self._inner.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def aclose(self) -> None:
        return None
