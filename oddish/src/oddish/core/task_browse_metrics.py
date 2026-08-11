from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import and_, case, func, or_

from oddish.config import settings
from oddish.core.cost_basis import not_combine_copy_filter
from oddish.db import TrialModel, TrialStatus
from oddish.model_pricing import estimate_cost_usd

_AGENT_TIMEOUT_LIKE = ("%AgentTimeoutError%", "%Agent execution timed out%")


def browse_trial_scope() -> tuple[Any, ...]:
    """Canonical trial population for normal task-browser cards."""
    return (
        TrialModel.deleted_at.is_(None),
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.is_probe.isnot(True),
        not_combine_copy_filter(),
    )


def trial_bucket_label() -> Any:
    """SQL reward/error bucket matching frontend ``getMatrixStatus``."""
    is_timeout = and_(
        TrialModel.error_message.isnot(None),
        or_(*(TrialModel.error_message.like(p) for p in _AGENT_TIMEOUT_LIKE)),
    )
    has_reward = TrialModel.reward.isnot(None)
    reward_bucket = case(
        (TrialModel.reward == 1, "pass"),
        (TrialModel.reward == 0, "fail"),
        else_="partial",
    )
    return case(
        (TrialModel.status == TrialStatus.SKIPPED, "skipped"),
        (
            and_(TrialModel.error_message.isnot(None), ~and_(is_timeout, has_reward)),
            "harness",
        ),
        (
            TrialModel.status == TrialStatus.FAILED,
            case((and_(is_timeout, has_reward), reward_bucket), else_="harness"),
        ),
        (
            TrialModel.status == TrialStatus.SUCCESS,
            case((has_reward, reward_bucket), else_="scoreless"),
        ),
        else_="other",
    )


def browse_cost_group_columns() -> list[Any]:
    """Raw model-grouped cost inputs; pricing stays current at read time."""
    input_tokens = func.greatest(func.coalesce(TrialModel.input_tokens, 0), 0)
    output_tokens = func.greatest(func.coalesce(TrialModel.output_tokens, 0), 0)
    cache_tokens = func.greatest(func.coalesce(TrialModel.cache_tokens, 0), 0)
    cache_write = func.greatest(func.coalesce(TrialModel.cache_write_tokens, 0), 0)
    has_tokens = and_(
        TrialModel.cost_usd.is_(None),
        or_(input_tokens > 0, output_tokens > 0, cache_write > 0),
    )
    estimator_input = func.greatest(input_tokens - cache_tokens - cache_write, 0)
    estimator_input += cache_tokens + cache_write
    return [
        func.sum(
            case((TrialModel.cost_usd.isnot(None), TrialModel.cost_usd), else_=0.0)
        ).label("native_cost"),
        func.count(case((TrialModel.cost_usd.isnot(None), 1))).label("native_trials"),
        func.sum(case((has_tokens, estimator_input), else_=0)).label("est_input"),
        func.sum(case((has_tokens, output_tokens), else_=0)).label("est_output"),
        func.sum(case((has_tokens, cache_tokens), else_=0)).label("est_cache"),
        func.sum(case((has_tokens, cache_write), else_=0)).label("est_cache_write"),
        func.count(case((has_tokens, 1))).label("est_trials"),
    ]


def resolve_browse_cost_breakdown(
    groups: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "cost_usd": 0.0,
        "cost_trial_count": 0,
        "cost_has_estimated": False,
        "cost_has_native": False,
        "billed_cost_usd": 0.0,
        "billed_trial_count": 0,
        "billed_has_estimated": False,
        "billed_has_native": False,
    }
    for group in groups or ():
        native = float(group.get("native_cost") or 0.0)
        native_trials = int(group.get("native_trials") or 0)
        model = settings.normalize_trial_model(
            str(group.get("agent") or ""), group.get("model"), strict=False
        ) or group.get("model")
        estimated = estimate_cost_usd(
            model,
            group.get("est_input"),
            group.get("est_output"),
            group.get("est_cache"),
            group.get("est_cache_write"),
        )
        estimated_trials = (
            int(group.get("est_trials") or 0) if estimated is not None else 0
        )
        targets = ("cost", "billed") if group.get("billed") else ("cost",)
        for target in targets:
            totals[f"{target}_usd"] += native + float(estimated or 0.0)
            totals[f"{target}_trial_count"] += native_trials + estimated_trials
            totals[f"{target}_has_native"] |= native_trials > 0
            totals[f"{target}_has_estimated"] |= estimated_trials > 0
    return totals
