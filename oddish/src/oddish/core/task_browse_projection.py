from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from oddish.core.task_browse_summary import (
    BROWSE_TRIAL_PREVIEW_LIMIT,
    DEFAULT_BROWSE_MODEL_KEY,
    build_task_version_browse_summary,
)

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
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    model_counts: dict[str, int] = {}
    for source in (persisted, live):
        for group in source.get("trial_groups", []):
            agent = str(group["agent"])
            model_key = str(group["model_key"])
            key = (agent, model_key)
            target = groups.setdefault(
                key,
                {
                    "agent": agent,
                    "model_key": model_key,
                    "trial_count": 0,
                    "reward_sum": 0.0,
                    "reward_total": 0,
                },
            )
            for field in ("trial_count", "reward_sum", "reward_total"):
                target[field] += group.get(field, 0)
    for agent, _ in groups:
        model_counts[agent] = model_counts.get(agent, 0) + 1
    for (agent, model_key), group in groups.items():
        group["model_label"] = (
            model_key
            if model_key != DEFAULT_BROWSE_MODEL_KEY or model_counts[agent] > 1
            else None
        )
    merged["trial_groups"] = list(groups.values())
    previews = [
        dict(preview)
        for source in (persisted, live)
        for preview in source.get("latest_trials", [])
    ]
    previews.sort(key=lambda preview: (preview.get("_created_at", ""), preview["id"]))
    merged["latest_trials"] = previews[-BROWSE_TRIAL_PREVIEW_LIMIT:]
    return merged
