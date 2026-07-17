"""Pure helpers for recording analysis-job LLM spend.

No DB or IO here so the logic is unit-testable in isolation; the ORM row is
constructed but not persisted (that is the caller's transaction).
"""

from __future__ import annotations

from dataclasses import dataclass

from oddish.db.models import AnalysisCostModel


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


def build_analysis_cost_row(
    *,
    job_kind: str,
    trial_id: str | None,
    org_id: str | None,
    experiment_id: str | None,
    billed_user_id: str | None,
    usage: AnalysisUsage,
) -> AnalysisCostModel:
    return AnalysisCostModel(
        job_kind=job_kind,
        trial_id=trial_id,
        org_id=org_id,
        experiment_id=experiment_id,
        billed_user_id=billed_user_id,
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
