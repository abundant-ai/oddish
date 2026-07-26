"""Pure helpers for recording analysis-job LLM spend.

No DB or IO here so the logic is unit-testable in isolation; the ORM row is
constructed but not persisted (that is the caller's transaction).
"""

from __future__ import annotations

from dataclasses import dataclass

from oddish.db.models import AnalysisCostModel
from oddish.model_pricing import estimate_cost_usd


@dataclass(frozen=True)
class AnalysisUsage:
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    model: str | None
    source: str  # "native" | "estimated"


def parse_cli_usage(payload: dict, model_id: str | None) -> AnalysisUsage | None:
    """Extract usage from a Claude Code ``--output-format json`` envelope.

    Returns ``None`` when the envelope carries no ``total_cost_usd`` — there is
    no cost signal to record, and we never fabricate one.
    """
    total = payload.get("total_cost_usd")
    if total is None:
        return None
    usage = payload.get("usage") or {}
    return AnalysisUsage(
        cost_usd=float(total),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_write_tokens=usage.get("cache_creation_input_tokens"),
        model=model_id,
        source="native",
    )


def usage_from_api_message(
    usage: object | None, model: str | None
) -> AnalysisUsage | None:
    """Extract usage from an Anthropic Messages API final message's ``usage``.

    Unlike the Claude Code CLI envelope, the API reports token counts but no
    dollar figure, so cost is priced from ``model_pricing`` and the row is
    marked ``"estimated"``. Returns ``None`` when no tokens were reported.

    ``input_tokens`` is recorded as the *total* input -- uncached + cache read +
    cache write -- because that is what ``estimate_cost_usd`` and the trials
    table mean by the field. The API reports the uncached part alone, with the
    two cache counts additive, so they are summed back together here.
    """
    if usage is None:
        return None

    def _count(name: str) -> int | None:
        value = getattr(usage, name, None)
        return int(value) if isinstance(value, (int, float)) else None

    uncached = _count("input_tokens")
    output = _count("output_tokens")
    cache_read = _count("cache_read_input_tokens")
    cache_write = _count("cache_creation_input_tokens")

    parts = [p for p in (uncached, cache_read, cache_write) if p is not None]
    total_input = sum(parts) if parts else None
    if total_input is None and output is None:
        return None

    return AnalysisUsage(
        cost_usd=estimate_cost_usd(model, total_input, output, cache_read, cache_write),
        input_tokens=total_input,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        model=model,
        source="estimated",
    )


def usage_from_openai_completion(
    usage: object | None, model: str | None
) -> AnalysisUsage | None:
    """Extract usage from an OpenAI chat completion's ``usage``.

    Mirrors :func:`usage_from_api_message` for the OpenAI path, with one
    inverted convention: OpenAI's ``prompt_tokens`` *already includes* the
    cached tokens that ``prompt_tokens_details.cached_tokens`` reports, whereas
    Anthropic reports uncached input with its cache counts additive. So the
    total is taken as-is here -- summing them would double-count the cache and
    overprice every row. There is no prompt-cache *write* concept on this path.
    """
    if usage is None:
        return None

    def _count(value: object) -> int | None:
        return int(value) if isinstance(value, (int, float)) else None

    total_input = _count(getattr(usage, "prompt_tokens", None))
    output = _count(getattr(usage, "completion_tokens", None))
    cache_read = _count(
        getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
    )
    if total_input is None and output is None:
        return None

    return AnalysisUsage(
        cost_usd=estimate_cost_usd(model, total_input, output, cache_read, None),
        input_tokens=total_input,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=None,
        model=model,
        source="estimated",
    )


def build_analysis_cost_row(
    *,
    job_kind: str,
    trial_id: str | None,
    org_id: str | None,
    experiment_id: str | None,
    billed_user_id: str | None,
    usage: AnalysisUsage,
    task_id: str | None = None,
    analyzer_id: str | None = None,
) -> AnalysisCostModel:
    return AnalysisCostModel(
        job_kind=job_kind,
        trial_id=trial_id,
        org_id=org_id,
        experiment_id=experiment_id,
        billed_user_id=billed_user_id,
        task_id=task_id,
        analyzer_id=analyzer_id,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        cost_source=usage.source,
    )


def should_record_cost(
    classification_result: object | None, usage: AnalysisUsage | None
) -> bool:
    """Record a cost row only when the analysis stored AND we captured usage."""
    return classification_result is not None and usage is not None
