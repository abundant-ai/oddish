from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from oddish.core.task_browse_status import status_value
from oddish.core.task_browse_summary import (
    BROWSE_TRIAL_PREVIEW_LIMIT,
    build_task_version_browse_summary,
)

_MUTABLE_STATUSES = frozenset(("running", "retrying"))
_ADDITIVE_FIELDS = (
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
_FLAG_FIELDS = (
    "cost_has_estimated",
    "cost_has_native",
    "billed_has_estimated",
    "billed_has_native",
)


def build_task_version_browse_projection(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[datetime | None, dict[str, Any]]:
    """Build persisted stable totals while retaining all-trial recency."""
    all_rows = list(rows)
    last_run_at, _ = build_task_version_browse_summary(all_rows)
    stable_rows = [
        row for row in all_rows if status_value(row["status"]) not in _MUTABLE_STATUSES
    ]
    _, summary = build_task_version_browse_summary(stable_rows)
    return last_run_at, summary


def merge_task_version_browse_summaries(
    persisted: Mapping[str, Any], live: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge persisted stable totals with page-scoped mutable trials."""
    _, merged = build_task_version_browse_summary(())
    for field in _ADDITIVE_FIELDS:
        merged[field] = persisted.get(field, 0) + live.get(field, 0)
    for field in _FLAG_FIELDS:
        merged[field] = bool(persisted.get(field) or live.get(field))
    merged["trial_status_counts"] = {
        key: int(persisted.get("trial_status_counts", {}).get(key, 0))
        + int(live.get("trial_status_counts", {}).get(key, 0))
        for key in merged["trial_status_counts"]
    }
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    for source in (persisted, live):
        for group in source.get("trial_groups", []):
            key = (str(group["agent"]), group.get("model"))
            target = groups.setdefault(
                key,
                {
                    "agent": key[0],
                    "model": key[1],
                    "trial_count": 0,
                    "reward_sum": 0.0,
                    "reward_total": 0,
                },
            )
            for field in ("trial_count", "reward_sum", "reward_total"):
                target[field] += group.get(field, 0)
    merged["trial_groups"] = list(groups.values())
    previews = [
        dict(preview)
        for source in (persisted, live)
        for preview in source.get("latest_trials", [])
    ]
    previews.sort(key=lambda preview: (preview.get("_created_at", ""), preview["id"]))
    merged["latest_trials"] = previews[-BROWSE_TRIAL_PREVIEW_LIMIT:]
    return merged
