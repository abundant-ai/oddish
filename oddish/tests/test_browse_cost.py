"""Unit tests for the task-browser per-task cost resolution.

``_resolve_browse_trial_cost`` mirrors ``helpers._resolve_trial_cost``:
prefer the agent's native ``cost_usd``, else token-estimate (CLI agents
report tokens but no native cost), else unpriceable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.dialects import postgresql

from oddish.core.endpoints.tasks_query import (
    _AGGREGATE_SORTS,
    _resolve_browse_trial_cost,
    _task_metrics_subquery,
)


def _row(**kw):
    base = {
        "cost_usd": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_tokens": None,
        "cache_write_tokens": None,
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


def test_cache_write_tokens_with_known_model_are_estimated() -> None:
    cost, estimated = _resolve_browse_trial_cost(
        _row(
            model="gpt-5.5-codex",
            cache_write_tokens=1_000_000,
        )
    )
    assert cost is not None and abs(cost - 6.25) < 1e-9
    assert estimated is True


def test_cost_desc_scopes_persisted_and_estimated_cost_to_model_and_finish_time() -> None:
    metrics = _task_metrics_subquery(
        "org-1",
        cost_models=["gpt-5.5-codex"],
        cost_finished_after=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    sql = str(
        metrics.select().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()

    assert _AGGREGATE_SORTS["cost_desc"] == ("cost_usd", True)
    assert "trials.model in ('gpt-5.5-codex')" in sql
    assert "trials.finished_at >= '2026-07-02" in sql
    assert "trials.cost_usd is not null" in sql
    assert "trials.cache_write_tokens" in sql
