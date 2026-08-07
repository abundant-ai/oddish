"""SQL predicates for the canonical trial metric filter contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import Integer, and_, exists, false, func, select

from oddish.db import TrialModel, TrialStatus
from oddish.filters.trial_metrics import TrialMetricFilter, TrialMetricMatch


@dataclass(frozen=True)
class EligibleTrialScope:
    """Membership clauses supplied by the surface using the filter."""

    membership: Sequence[Any]
    include_probes: bool = False
    include_superseded: bool = False
    include_deleted: bool = False
    include_non_agent_kinds: bool = False

    def clauses(self) -> list[Any]:
        clauses = list(self.membership)
        if not self.include_non_agent_kinds:
            clauses.append(TrialModel.kind == "agent")
        if not self.include_probes:
            clauses.append(TrialModel.is_probe.isnot(True))
        if not self.include_superseded:
            clauses.append(TrialModel.superseded_by_trial_id.is_(None))
        if not self.include_deleted:
            clauses.append(TrialModel.deleted_at.is_(None))
        return clauses


def trial_metric_conditions(metric_filter: TrialMetricFilter) -> list[Any]:
    """The metric clauses that must all hold on one trial."""
    conditions: list[Any] = []
    if metric_filter.min_steps is not None:
        conditions.append(TrialModel.total_steps >= metric_filter.min_steps)
    if metric_filter.max_steps is not None:
        conditions.append(TrialModel.total_steps <= metric_filter.max_steps)
    if metric_filter.min_duration_seconds is not None:
        conditions.append(
            TrialModel.trajectory_duration_seconds >= metric_filter.min_duration_seconds
        )
    if metric_filter.max_duration_seconds is not None:
        conditions.append(
            TrialModel.trajectory_duration_seconds <= metric_filter.max_duration_seconds
        )
    if metric_filter.min_tool_calls is not None:
        conditions.append(TrialModel.total_tool_calls >= metric_filter.min_tool_calls)
    if metric_filter.max_tool_calls is not None:
        conditions.append(TrialModel.total_tool_calls <= metric_filter.max_tool_calls)
    for name in metric_filter.tool_names:
        conditions.append(TrialModel.tool_counts.has_key(name))
    for name, minimum in metric_filter.tool_count_mins.items():
        conditions.append(
            func.coalesce(TrialModel.tool_counts[name].astext.cast(Integer), 0)
            >= minimum
        )
    return conditions


def trial_metric_measurement_clauses(
    metric_filter: TrialMetricFilter,
) -> list[Any]:
    """Clauses identifying trials with every requested scalar measurement."""
    clauses: list[Any] = []
    if metric_filter.min_steps is not None or metric_filter.max_steps is not None:
        clauses.append(TrialModel.total_steps.isnot(None))
    if (
        metric_filter.min_duration_seconds is not None
        or metric_filter.max_duration_seconds is not None
    ):
        clauses.append(TrialModel.trajectory_duration_seconds.isnot(None))
    if (
        metric_filter.min_tool_calls is not None
        or metric_filter.max_tool_calls is not None
    ):
        clauses.append(TrialModel.total_tool_calls.isnot(None))
    return clauses


def build_trial_metric_predicate(
    metric_filter: TrialMetricFilter,
    *,
    scope: EligibleTrialScope,
) -> Any | None:
    """Build non-vacuous Any/All semantics over one eligible trial scope."""
    if metric_filter.is_empty:
        return None

    eligible = scope.clauses()
    if metric_filter.models:
        eligible.append(TrialModel.model.in_(metric_filter.models))
    conditions = trial_metric_conditions(metric_filter)
    # Only completed trials with every requested scalar measurement participate
    # in metric matching. Running/unmeasured trials and execution errors are
    # ignored, while eligible_exists keeps All mode non-vacuous.
    eligible.extend(
        [
            TrialModel.status == TrialStatus.SUCCESS,
            *trial_metric_measurement_clauses(metric_filter),
        ]
    )
    eligible_exists = exists(select(TrialModel.id).where(and_(*eligible)))
    if not conditions:
        return eligible_exists
    if metric_filter.match is TrialMetricMatch.ANY:
        return exists(select(TrialModel.id).where(and_(*eligible, *conditions)))

    failing_exists = exists(
        select(TrialModel.id).where(
            and_(*eligible, ~func.coalesce(and_(*conditions), false()))
        )
    )
    return and_(eligible_exists, ~failing_exists)
