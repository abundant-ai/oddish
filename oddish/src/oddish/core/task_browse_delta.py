from __future__ import annotations

from collections.abc import Mapping

from oddish.core.task_browse_contribution import TrialBrowseContribution

Number = int | float
_STATUS_COLUMNS = {
    "pass": "pass_count",
    "partial": "partial_count",
    "fail": "fail_count",
    "harness-error": "harness_error_count",
    "scoreless": "scoreless_count",
    "skipped": "skipped_count",
    "pending": "pending_count",
    "queued": "queued_count",
    "running": "running_count",
}


def contribution_delta(item: TrialBrowseContribution, sign: int) -> dict[str, Number]:
    if item.is_mutable:
        return {}
    reward = item.reward
    delta: dict[str, Number] = {
        "total_trials": sign,
        "completed_trials": sign * int(item.status == "success"),
        "failed_trials": sign * int(item.status == "failed"),
        "reward_success": sign * int(reward == 1),
        "reward_sum": sign * (reward or 0.0),
        "reward_total": sign * int(reward is not None),
        _STATUS_COLUMNS[item.status_bucket]: sign,
        "total_tokens": sign * item.total_tokens,
    }
    if item.runtime_seconds is not None:
        delta["runtime_total"] = sign * item.runtime_seconds
        delta["runtime_count"] = sign
    if item.resolved_cost_usd is not None:
        delta["cost_usd"] = sign * item.resolved_cost_usd
        delta["cost_trial_count"] = sign
        delta[
            "cost_estimated_count" if item.cost_estimated else "cost_native_count"
        ] = sign
        if item.billed:
            delta["billed_cost_usd"] = sign * item.resolved_cost_usd
            delta["billed_trial_count"] = sign
            delta[
                "billed_estimated_count"
                if item.cost_estimated
                else "billed_native_count"
            ] = sign
    return delta


def group_delta(item: TrialBrowseContribution, sign: int) -> dict[str, Number]:
    if item.is_mutable:
        return {}
    delta: dict[str, Number] = {
        "trial_count": sign,
        "pass_count": sign * int(item.status_bucket == "pass"),
        "reward_sum": sign * (item.reward or 0.0),
        "reward_total": sign * int(item.reward is not None),
        "token_sum": sign * item.total_tokens * int(item.tokens_known),
        "token_count": sign * int(item.tokens_known),
        "runtime_sum": sign * (item.runtime_seconds or 0.0),
        "runtime_count": sign * int(item.runtime_seconds is not None),
        "steps_sum": sign * (item.total_steps or 0),
        "steps_count": sign * int(item.total_steps is not None),
    }
    if item.resolved_cost_usd is not None:
        delta["cost_usd"] = sign * item.resolved_cost_usd
    return delta


def metric_values(item: TrialBrowseContribution) -> dict[str, float]:
    if item.is_mutable:
        return {}
    values: dict[str, float] = {}
    if item.reward is not None:
        values["reward"] = item.reward
    if item.runtime_seconds is not None:
        values["runtime"] = item.runtime_seconds
    if item.tokens_known:
        values["tokens"] = float(item.total_tokens)
    if item.total_steps is not None:
        values["steps"] = float(item.total_steps)
    return values


def add_delta(target: dict[str, Number], delta: Mapping[str, Number]) -> None:
    for field, value in delta.items():
        target[field] = target.get(field, 0) + value
