"""Unit tests for the task-browser per-task cost resolution.

``_resolve_browse_trial_cost`` mirrors ``helpers._resolve_trial_cost``:
prefer the agent's native ``cost_usd``, else token-estimate (CLI agents
report tokens but no native cost), else unpriceable.
"""

from __future__ import annotations

from oddish.core.endpoints import _resolve_browse_trial_cost


def _row(**kw):
    base = {
        "cost_usd": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_tokens": None,
        "agent": "codex",
        "model": None,
    }
    base.update(kw)
    return base


def test_native_cost_is_used_verbatim() -> None:
    cost, estimated = _resolve_browse_trial_cost(_row(cost_usd=0.5))
    assert cost == 0.5
    assert estimated is False


def test_no_tokens_and_no_cost_is_unpriceable() -> None:
    cost, estimated = _resolve_browse_trial_cost(_row())
    assert cost is None
    assert estimated is False


def test_tokens_with_known_model_are_estimated() -> None:
    # gpt-5.5-codex: $5/M input. 1M input -> $5.00, flagged estimated.
    cost, estimated = _resolve_browse_trial_cost(
        _row(
            agent="codex",
            model="gpt-5.5-codex",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_tokens=0,
        )
    )
    assert cost is not None and abs(cost - 5.0) < 1e-9
    assert estimated is True


def test_tokens_with_unknown_model_is_unpriceable() -> None:
    cost, estimated = _resolve_browse_trial_cost(
        _row(
            agent="codex",
            model="totally-made-up-model-xyz",
            input_tokens=1_000,
            output_tokens=1_000,
        )
    )
    assert cost is None
    assert estimated is False
