from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from oddish.db import TaskModel, TrialModel
from oddish.filters.trial_metrics import TrialMetricFilter, TrialMetricMatch
from oddish.filters.trial_predicates import (
    EligibleTrialScope,
    build_trial_metric_predicate,
)


def test_query_contract_parses_and_serializes_canonically() -> None:
    metric_filter = TrialMetricFilter.from_query(
        models="openai/gpt-5,anthropic/claude",
        min_steps=100,
        max_duration_seconds=300,
        tool_names="bash,read_file,bash",
        tool_count_mins='{"bash": 5}',
        match="all",
    )

    assert metric_filter.models == ["openai/gpt-5", "anthropic/claude"]
    assert metric_filter.tool_names == ["bash", "read_file"]
    assert metric_filter.match is TrialMetricMatch.ALL
    assert metric_filter.to_query_params() == {
        "models": "openai/gpt-5,anthropic/claude",
        "min_steps": "100",
        "max_duration_seconds": "300.0",
        "tool_names": "bash,read_file",
        "tool_count_mins": '{"bash":5}',
        "trial_metric_match": "all",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_steps": 10, "max_steps": 5},
        {"tool_count_mins": '{"bash": -1}'},
        {"tool_count_mins": "not-json"},
        {"match": "sometimes"},
    ],
)
def test_query_contract_rejects_invalid_values(kwargs: dict) -> None:
    with pytest.raises((ValidationError, ValueError)):
        TrialMetricFilter.from_query(**kwargs)


def _compiled(match: str) -> str:
    metric_filter = TrialMetricFilter.from_query(
        models=["openai/gpt-5"], min_steps=100, match=match
    )
    predicate = build_trial_metric_predicate(
        metric_filter,
        scope=EligibleTrialScope(
            membership=(
                TrialModel.task_id == TaskModel.id,
                TrialModel.task_version_id == TaskModel.current_version_id,
            )
        ),
    )
    assert predicate is not None
    return str(
        select(TaskModel.id)
        .where(predicate)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_any_predicate_keeps_model_and_metrics_in_one_exists() -> None:
    sql = _compiled("any")
    assert sql.count("EXISTS") == 1
    assert "trials.model IN ('openai/gpt-5')" in sql
    assert "trials.total_steps >= 100" in sql
    assert "trials.task_version_id = tasks.current_version_id" in sql


def test_all_predicate_is_non_vacuous_and_rejects_missing_metrics() -> None:
    sql = _compiled("all")
    assert sql.count("EXISTS") == 2
    assert "coalesce(trials.total_steps >= 100, false)" in sql
    assert "NOT (EXISTS" in sql
