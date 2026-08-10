from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from oddish.core.task_browse_cost import resolve_browse_trial_cost
from oddish.core.task_browse_status import (
    BROWSE_STATUS_KEYS,
    matrix_status,
    status_value,
)

BROWSE_TRIAL_PREVIEW_LIMIT = 100


def _empty_summary() -> dict[str, Any]:
    return {
        "total_trials": 0,
        "completed_trials": 0,
        "failed_trials": 0,
        "reward_success": 0,
        "reward_sum": 0.0,
        "reward_total": 0,
        "cost_usd": 0.0,
        "cost_trial_count": 0,
        "cost_has_estimated": False,
        "cost_has_native": False,
        "billed_cost_usd": 0.0,
        "billed_trial_count": 0,
        "billed_has_estimated": False,
        "billed_has_native": False,
        "trial_status_counts": dict.fromkeys(BROWSE_STATUS_KEYS, 0),
        "trial_groups": [],
        "latest_trials": [],
    }


def build_task_version_browse_summary(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[datetime | None, dict[str, Any]]:
    """Fold eligible trials for one task version into its browse projection."""
    summary = _empty_summary()
    previews: deque[dict[str, Any]] = deque(maxlen=BROWSE_TRIAL_PREVIEW_LIMIT)
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    last_run_at: datetime | None = None
    for row in rows:
        status = status_value(row["status"])
        reward = row["reward"]
        summary["total_trials"] += 1
        summary["completed_trials"] += status == "success"
        summary["failed_trials"] += status == "failed"
        if reward is not None:
            summary["reward_success"] += reward == 1
            summary["reward_sum"] += float(reward)
            summary["reward_total"] += 1
        activity = max(
            value
            for value in (row["finished_at"], row["started_at"], row["created_at"])
            if value is not None
        )
        last_run_at = activity if last_run_at is None else max(last_run_at, activity)
        summary["trial_status_counts"][matrix_status(row)] += 1
        key = (str(row["agent"]), row["model"])
        group = groups.setdefault(
            key,
            {
                "agent": key[0],
                "model": key[1],
                "trial_count": 0,
                "reward_sum": 0.0,
                "reward_total": 0,
            },
        )
        group["trial_count"] += 1
        if reward is not None:
            group["reward_sum"] += float(reward)
            group["reward_total"] += 1
        cost, estimated = resolve_browse_trial_cost(row)
        if cost is not None:
            _add_cost(
                summary, cost, estimated, billed=row["billed_user_id"] is not None
            )
        previews.append(
            {
                key: row[key]
                for key in ("id", "name", "reward", "error_message", "agent", "model")
            }
            | {"status": status, "_created_at": row["created_at"].isoformat()}
        )
    summary["trial_groups"] = list(groups.values())
    summary["latest_trials"] = list(previews)
    return last_run_at, summary


def _add_cost(
    summary: dict[str, Any], cost: float, estimated: bool, *, billed: bool
) -> None:
    summary["cost_usd"] += cost
    summary["cost_trial_count"] += 1
    summary["cost_has_estimated" if estimated else "cost_has_native"] = True
    if billed:
        summary["billed_cost_usd"] += cost
        summary["billed_trial_count"] += 1
        summary["billed_has_estimated" if estimated else "billed_has_native"] = True
