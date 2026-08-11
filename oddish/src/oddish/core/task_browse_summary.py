from __future__ import annotations

from collections import defaultdict, deque
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
DEFAULT_BROWSE_MODEL_KEY = "default"


def browse_trial_model_key(model: object) -> str:
    """Canonical model identity for task-browser agent/model cohorts."""
    value = str(model).strip() if model is not None else ""
    return value or DEFAULT_BROWSE_MODEL_KEY


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
    groups: dict[tuple[str, str], dict[str, Any]] = {}
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
        agent = str(row["agent"])
        model_key = browse_trial_model_key(row["model"])
        key = (agent, model_key)
        group = groups.setdefault(
            key,
            {
                "agent": agent,
                "model_key": model_key,
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
    model_counts: dict[str, int] = defaultdict(int)
    for agent, _ in groups:
        model_counts[agent] += 1
    for (agent, model_key), group in groups.items():
        group["model_label"] = (
            model_key
            if model_key != DEFAULT_BROWSE_MODEL_KEY or model_counts[agent] > 1
            else None
        )
    summary["trial_groups"] = list(groups.values())
    summary["latest_trials"] = list(previews)
    return last_run_at, summary


def build_task_browse_trial_groups(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Nest bounded trial previews under the backend-owned cohort identity."""
    previews: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for preview in summary.get("latest_trials", []):
        key = (
            str(preview["agent"]),
            browse_trial_model_key(preview.get("model")),
        )
        previews[key].append(dict(preview))
    return [
        dict(group)
        | {"latest_trials": previews[(str(group["agent"]), str(group["model_key"]))]}
        for group in summary.get("trial_groups", [])
    ]


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
