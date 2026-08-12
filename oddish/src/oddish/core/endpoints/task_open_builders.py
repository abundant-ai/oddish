"""Compact row-to-schema mappers for the task-open resource."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from oddish.config import settings
from oddish.model_pricing import estimate_cost_usd
from oddish.schemas import (
    TaskBrowseExperiment,
    TaskOpenTrialRef,
    TaskOpenVerdict,
    UserTagRef,
)

TASK_OPEN_TRIAL_LIMIT = 20
_VERDICT_TEXT_LIMIT = 2_000
_VERDICT_ERROR_LIMIT = 4_000
_RECOMMENDATION_LIMIT = 8
_RECOMMENDATION_TEXT_LIMIT = 1_000


def tags(value: Any) -> list[UserTagRef]:
    return [UserTagRef.model_validate(item) for item in (value or [])]


def experiments(value: Any) -> list[TaskBrowseExperiment]:
    return [TaskBrowseExperiment.model_validate(item) for item in (value or [])]


def github_meta(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict) or not value.get("github_meta"):
        return None
    raw = value["github_meta"]
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): str(item)[:500] for key, item in list(parsed.items())[:20]}


def bounded_text(value: Any, limit: int = _VERDICT_ERROR_LIMIT) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def compact_verdict(value: Any) -> TaskOpenVerdict | None:
    if not isinstance(value, Mapping):
        return None
    recommendations = value.get("recommendations")
    compact_recommendations = []
    if isinstance(recommendations, list):
        compact_recommendations = [
            str(item)[:_RECOMMENDATION_TEXT_LIMIT]
            for item in recommendations[:_RECOMMENDATION_LIMIT]
        ]
    is_good = value.get("is_good")
    return TaskOpenVerdict(
        is_good=is_good if isinstance(is_good, bool) else None,
        confidence=bounded_text(value.get("confidence"), 32),
        primary_issue=bounded_text(value.get("primary_issue"), _VERDICT_TEXT_LIMIT),
        reasoning=bounded_text(value.get("reasoning"), _VERDICT_TEXT_LIMIT),
        recommendations=compact_recommendations,
    )


def trial_refs(rows: list[Mapping[str, Any]]) -> list[TaskOpenTrialRef]:
    refs = []
    for row in rows[:TASK_OPEN_TRIAL_LIMIT]:
        model = settings.normalize_trial_model(
            str(row["agent"]), row["model"], strict=False
        )
        cost, estimated = row["cost_usd"], False
        if cost is None:
            cost = estimate_cost_usd(
                model or row["model"],
                row["input_tokens"],
                row["output_tokens"],
                row["cache_tokens"],
                row["cache_write_tokens"],
            )
            estimated = True if cost is not None else None
        refs.append(
            TaskOpenTrialRef(
                id=str(row["id"]),
                name=str(row["name"]),
                experiment_id=row["experiment_id"],
                task_version_id=row["task_version_id"],
                agent=str(row["agent"]),
                provider=str(row["provider"]),
                model=model,
                status=row["status"],
                reward=row["reward"],
                error_kind=row["error_kind"],
                is_probe=bool(row["is_probe"]),
                cost_usd=float(cost) if cost is not None else None,
                cost_is_estimated=estimated,
                is_billed=row["billed_user_id"] is not None,
                created_at=row["created_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
        )
    return refs
