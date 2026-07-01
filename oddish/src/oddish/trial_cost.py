from __future__ import annotations

from oddish.config import settings
from oddish.model_pricing import estimate_cost_usd


def apply_settled_cost(trial, outcome=None) -> None:
    if outcome is not None:
        trial.input_tokens = outcome.input_tokens
        trial.cache_tokens = outcome.cache_tokens
        trial.cache_write_tokens = outcome.cache_write_tokens
        trial.output_tokens = outcome.output_tokens
        trial.total_steps = outcome.total_steps
        trial.cost_usd = (
            outcome.cost_usd
            if outcome.cost_usd is not None
            else _estimate_or_floor(trial)
        )
    elif trial.cost_usd is None:
        trial.cost_usd = _estimate_or_floor(trial)


def _estimate_or_floor(trial) -> float:
    try:
        estimated_cost_usd = estimate_cost_usd(
            trial.model,
            trial.input_tokens,
            trial.output_tokens,
            trial.cache_tokens,
            trial.cache_write_tokens,
        )
    except Exception:
        estimated_cost_usd = None
    if estimated_cost_usd is not None:
        return estimated_cost_usd
    return float(settings.pending_trial_reservation_usd)
