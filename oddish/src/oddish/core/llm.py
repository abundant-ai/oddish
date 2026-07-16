"""Single funnel for internal (non-harbor-trial) LLM calls.

Every internal completion goes through :func:`complete`: one client (litellm),
one retry policy, and one usage/cost row per call, attributed by ``handler``.
Harness traffic inside harbor trial containers does NOT go through here — it is
cost-settled at trial completion (see ``workers/harbor/outcome.py``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

COST_BASIS_API = "api"
COST_BASIS_OAUTH_FLAT = "oauth_flat"
COST_BASIS_UNPRICED = "unpriced"

# Models whose direct-API id supports output_config.format (structured outputs).
# Kept here (not litellm's stale substring gate, which lacks haiku-4-5/opus-4-8/
# fable-5): output_config is passed as a provider-specific param instead.
_STRUCTURED_OUTPUT_PREFIXES = (
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-8",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-fable-5",
)

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 8.0


@dataclass(frozen=True)
class LLMUsageRow:
    handler: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    cost_basis: str = COST_BASIS_API
    trial_id: str | None = None
    task_id: str | None = None
    experiment_id: str | None = None
    duration_ms: int | None = None
    success: bool = True
    error: str | None = None


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None


UsageRecorder = Callable[[LLMUsageRow], Awaitable[None]]

_recorder: UsageRecorder | None = None


def set_usage_recorder(recorder: UsageRecorder | None) -> None:
    """Install the process-wide usage sink (DB-backed in server/worker
    processes, absent in CLI processes). Mirrors
    ``observability.mark_observability_configured``."""
    global _recorder
    _recorder = recorder


async def record_usage(row: LLMUsageRow) -> None:
    """Best-effort write of one usage row; never raises into the caller."""
    if _recorder is None:
        return
    try:
        await _recorder(row)
    except Exception:
        logger.warning("llm_usage record failed (handler=%s)", row.handler, exc_info=True)


def supports_structured_outputs(model: str) -> bool:
    bare = model.split("/", 1)[1] if model.lower().startswith("anthropic/") else model
    return any(bare.startswith(p) for p in _STRUCTURED_OUTPUT_PREFIXES)


def _resolve_route(model: str, api_key: str | None) -> tuple[str, str, dict[str, Any]]:
    """Map a caller-facing model id to (litellm_model, pricing_model, call_kwargs).

    Claude ids (bare, ``anthropic/``-prefixed, or Bedrock inference-profile ids)
    always route to the direct Anthropic API — the workers hold no usable
    Bedrock inference credential (see ``worker/probe_analysis`` history). Other
    ids route to public OpenAI or the Azure OpenAI-compatible endpoint per
    settings, exactly mirroring the previous verdict client.
    """
    import warnings

    from oddish.config import (
        OPENAI_PROVIDER_OPENAI,
        settings,
        to_anthropic_api_model_id,
    )

    stripped = model.strip()
    lowered = stripped.lower()
    if lowered.startswith(("anthropic/", "claude/")):
        api_id = to_anthropic_api_model_id(stripped) or stripped
        return f"anthropic/{api_id}", api_id, {}
    if "/" in lowered and not lowered.startswith("bedrock/"):
        return stripped, stripped, {}
    api_id = to_anthropic_api_model_id(stripped) or stripped
    if api_id.lower().startswith("claude"):
        return f"anthropic/{api_id}", api_id, {}
    if settings.get_openai_provider() == OPENAI_PROVIDER_OPENAI:
        warnings.warn(settings.get_public_openai_warning(), stacklevel=2)
        public = settings.require_public_openai_config(api_key=api_key)
        return f"openai/{api_id}", api_id, {"api_key": public["api_key"]}
    azure = settings.require_azure_openai_config()
    deployment = settings.resolve_azure_openai_deployment(api_id)
    return (
        f"openai/{deployment}",
        api_id,
        {
            "api_key": azure["api_key"],
            "api_base": settings.get_azure_openai_base_url(),
        },
    )


def _extract_usage(response: Any) -> tuple[int, int, int, int]:
    """(input_total, output, cache_read, cache_write) from a litellm response.

    litellm folds cache read+write tokens INTO prompt_tokens, which is exactly
    the inclusive total ``model_pricing.estimate_cost_usd`` expects (it
    subtracts the cache counters internally). Do not pre-subtract here.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0, 0

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int(getattr(usage, "prompt_tokens", 0))
    completion = _int(getattr(usage, "completion_tokens", 0))
    details = getattr(usage, "prompt_tokens_details", None)
    cache_read = _int(getattr(details, "cached_tokens", 0)) if details is not None else 0
    cache_write = _int(getattr(usage, "cache_creation_input_tokens", 0))
    if not cache_write and details is not None:
        cache_write = _int(getattr(details, "cache_creation_tokens", 0))
    return prompt, completion, cache_read, cache_write


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    return isinstance(exc, (TimeoutError, ConnectionError))


async def _call_with_retry(call: Callable[[], Awaitable[Any]]) -> Any:
    delay = _BACKOFF_BASE_SECONDS
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == _MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            sleep_s = min(delay, _BACKOFF_MAX_SECONDS) * (0.5 + random.random() / 2)
            logger.warning(
                "llm call failed (attempt %d/%d, retrying in %.1fs): %s",
                attempt,
                _MAX_ATTEMPTS,
                sleep_s,
                exc,
            )
            await asyncio.sleep(sleep_s)
            delay *= 2


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "message", None), "content", None)
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""


async def complete(
    *,
    handler: str,
    prompt: str,
    model: str,
    max_tokens: int = 2048,
    temperature: float | None = None,
    schema: dict | None = None,
    response_format: Any | None = None,
    timeout: float = 120.0,
    api_key: str | None = None,
    trial_id: str | None = None,
    task_id: str | None = None,
    experiment_id: str | None = None,
) -> LLMResult:
    """Run one internal completion through litellm and record its usage.

    ``schema`` requests Anthropic-native structured outputs (silently skipped
    for models without support, matching the previous probe behavior);
    ``response_format`` passes through to litellm for OpenAI-family models
    (e.g. a pydantic model class).
    """
    import litellm

    litellm_model, pricing_model, route_kwargs = _resolve_route(model, api_key)
    kwargs: dict[str, Any] = {
        "model": litellm_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens,
        "timeout": timeout,
        **route_kwargs,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if schema is not None and supports_structured_outputs(litellm_model):
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": schema}
        }
    if response_format is not None:
        kwargs["response_format"] = response_format

    row = LLMUsageRow(
        handler=handler,
        model=pricing_model,
        trial_id=trial_id,
        task_id=task_id,
        experiment_id=experiment_id,
    )
    started = time.monotonic()
    try:
        response = await _call_with_retry(lambda: litellm.acompletion(**kwargs))
    except Exception as exc:
        await record_usage(
            replace(
                row,
                duration_ms=int((time.monotonic() - started) * 1000),
                success=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
        )
        raise

    input_tokens, output_tokens, cache_read, cache_write = _extract_usage(response)
    from oddish.model_pricing import settle_cost_usd

    cost_usd = settle_cost_usd(
        None,
        native_cost_trusted=False,
        model=pricing_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
    has_tokens = bool(input_tokens or output_tokens or cache_write)
    await record_usage(
        replace(
            row,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost_usd,
            cost_basis=(
                COST_BASIS_UNPRICED
                if cost_usd is None and has_tokens
                else COST_BASIS_API
            ),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )
    return LLMResult(
        text=_response_text(response),
        model=pricing_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost_usd,
    )
