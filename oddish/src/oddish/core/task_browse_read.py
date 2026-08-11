from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from oddish.core.task_browse_summary import DEFAULT_BROWSE_MODEL_KEY
from oddish.db import (
    TaskBrowseTrialProjectionModel,
    TaskVersionBrowseAgentRollupModel,
    TaskVersionBrowseRollupModel,
)

Row = Mapping[str, Any] | Any


def _value(row: Row, field: str) -> Any:
    return row[field] if isinstance(row, Mapping) else getattr(row, field)


def summary_from_rollup(
    rollup: TaskVersionBrowseRollupModel | Mapping[str, Any] | None,
    groups: Iterable[TaskVersionBrowseAgentRollupModel | Mapping[str, Any]] = (),
    previews: Iterable[TaskBrowseTrialProjectionModel | Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the transport-neutral card summary from normalized read rows."""
    if rollup is None:
        scalar = {
            field: 0
            for field in (
                "total_trials",
                "completed_trials",
                "failed_trials",
                "reward_success",
                "reward_total",
                "cost_trial_count",
                "billed_trial_count",
            )
        } | {"reward_sum": 0.0, "cost_usd": 0.0, "billed_cost_usd": 0.0}
        status_counts = dict.fromkeys(
            (
                "pass",
                "partial",
                "fail",
                "harness-error",
                "scoreless",
                "skipped",
                "pending",
                "queued",
                "running",
            ),
            0,
        )
        flags = {
            "cost_has_estimated": False,
            "cost_has_native": False,
            "billed_has_estimated": False,
            "billed_has_native": False,
        }
    else:
        scalar = {
            field: _value(rollup, field)
            for field in (
                "total_trials",
                "completed_trials",
                "failed_trials",
                "reward_success",
                "reward_sum",
                "reward_total",
                "cost_usd",
                "cost_trial_count",
                "billed_cost_usd",
                "billed_trial_count",
            )
        }
        status_counts = {
            "pass": _value(rollup, "pass_count"),
            "partial": _value(rollup, "partial_count"),
            "fail": _value(rollup, "fail_count"),
            "harness-error": _value(rollup, "harness_error_count"),
            "scoreless": _value(rollup, "scoreless_count"),
            "skipped": _value(rollup, "skipped_count"),
            "pending": _value(rollup, "pending_count"),
            "queued": _value(rollup, "queued_count"),
            "running": _value(rollup, "running_count"),
        }
        flags = {
            "cost_has_estimated": _value(rollup, "cost_estimated_count") > 0,
            "cost_has_native": _value(rollup, "cost_native_count") > 0,
            "billed_has_estimated": _value(rollup, "billed_estimated_count") > 0,
            "billed_has_native": _value(rollup, "billed_native_count") > 0,
        }
    group_rows = list(groups)
    model_counts = Counter(_value(group, "agent") for group in group_rows)
    trial_groups = [
        {
            "agent": _value(group, "agent"),
            "model_key": _value(group, "model_key"),
            "model_label": (
                _value(group, "model_key")
                if _value(group, "model_key") != DEFAULT_BROWSE_MODEL_KEY
                or model_counts[_value(group, "agent")] > 1
                else None
            ),
            "trial_count": _value(group, "trial_count"),
            "reward_sum": _value(group, "reward_sum"),
            "reward_total": _value(group, "reward_total"),
        }
        for group in group_rows
    ]
    latest_trials = [
        {
            "id": _value(preview, "trial_id"),
            "name": _value(preview, "name"),
            "status": _value(preview, "status"),
            "reward": _value(preview, "reward"),
            "error_message": _value(preview, "error_message"),
            "agent": _value(preview, "agent"),
            "model": _value(preview, "model"),
            "_created_at": _value(preview, "trial_created_at").isoformat(),
        }
        for preview in previews
    ]
    return (
        scalar
        | flags
        | {
            "trial_status_counts": status_counts,
            "trial_groups": trial_groups,
            "latest_trials": latest_trials,
        }
    )
