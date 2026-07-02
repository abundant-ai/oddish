from __future__ import annotations

from oddish.config import settings
from oddish.model_pricing import estimate_cost_usd


def apply_settled_cost(trial, outcome=None) -> None:
    if outcome is None:
        if trial.cost_usd is None:
            trial.cost_usd = _estimate_or_floor(trial)
        return

    # Once-only per attempt: at-least-once outcome redelivery must not
    # re-accumulate. cost_settled_attempt records the last settled attempt.
    attempts = trial.attempts or 0
    if attempts > 0 and (trial.cost_settled_attempt or 0) >= attempts:
        return

    # Attempts >1 already settled a prior attempt's cost; accumulate so a
    # retried trial bills every attempt's spend. Token fields stay last-attempt.
    prior_cost = trial.cost_usd if attempts > 1 else None
    trial.input_tokens = outcome.input_tokens
    trial.cache_tokens = outcome.cache_tokens
    trial.cache_write_tokens = outcome.cache_write_tokens
    trial.output_tokens = outcome.output_tokens
    trial.total_steps = outcome.total_steps
    resolved = (
        outcome.cost_usd if outcome.cost_usd is not None else _estimate_or_floor(trial)
    )
    trial.cost_usd = (prior_cost or 0) + resolved
    trial.cost_settled_attempt = attempts


def _estimate_or_floor(trial) -> float:
    try:
        cost = estimate_cost_usd(
            trial.model,
            trial.input_tokens,
            trial.output_tokens,
            trial.cache_tokens,
            trial.cache_write_tokens,
        )
    except Exception:
        cost = None
    return cost if cost is not None else float(settings.pending_trial_reservation_usd)
