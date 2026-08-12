"""Fold bounded task-open aggregate rows into exact response summaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from oddish.config import settings
from oddish.model_pricing import estimate_cost_usd
from oddish.schemas import (
    TaskOpenAgentModelSummary,
    TaskOpenTotals,
    TaskOpenVersionSummary,
)


def _add_cost(target: Any, row: Mapping[str, Any], *, billed: bool) -> None:
    normalized = settings.normalize_trial_model(
        str(row["agent"]), row.get("model"), strict=False
    )
    estimated = estimate_cost_usd(
        normalized or row.get("model"),
        int(row["estimated_input"] or 0),
        int(row["estimated_output"] or 0),
        int(row["estimated_cache"] or 0),
        int(row["estimated_cache_write"] or 0),
    )
    prefix = "billed_" if billed else ""
    count_field = "billed_trial_count" if billed else "cost_trial_count"
    native_field = "billed_has_native" if billed else "cost_has_native"
    estimated_field = "billed_has_estimated" if billed else "cost_has_estimated"
    native_count = int(row["native_count"] or 0)
    cost = float(row["native_cost"] or 0.0) + (estimated or 0.0)
    count = native_count + (
        int(row["estimated_count"] or 0) if estimated is not None else 0
    )
    setattr(target, f"{prefix}cost_usd", getattr(target, f"{prefix}cost_usd") + cost)
    setattr(target, count_field, getattr(target, count_field) + count)
    if native_count:
        setattr(target, native_field, True)
    if estimated is not None and row["estimated_count"]:
        setattr(target, estimated_field, True)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _add_counts(target: Any, row: Mapping[str, Any]) -> None:
    target.trial_count += int(row["total"] or 0)
    target.completed_count += int(row["completed"] or 0)
    target.failed_count += int(row["failed"] or 0)
    target.skipped_count += int(row["skipped"] or 0)
    target.pass_count += int(row["pass_count"] or 0)
    target.partial_count += int(row["partial_count"] or 0)
    target.fail_count += int(row["fail_count"] or 0)
    target.reward_sum += float(row["reward_sum"] or 0.0)
    target.reward_total += int(row["reward_total"] or 0)
    candidate = _datetime(row.get("last_run_at"))
    if candidate is not None and (
        target.last_run_at is None or candidate > target.last_run_at
    ):
        target.last_run_at = candidate


def _agent_key(row: Mapping[str, Any]) -> tuple[bool, str, str | None]:
    is_probe = bool(row["is_probe"])
    if is_probe:
        return True, "probe", None
    agent = str(row["agent"])
    model = settings.normalize_trial_model(agent, row.get("model"), strict=False)
    return False, agent, model


def _finalize(target: Any) -> None:
    target.pending_count = max(
        target.trial_count
        - target.completed_count
        - target.failed_count
        - target.skipped_count,
        0,
    )


def fold_task_open_groups(
    rows: list[Mapping[str, Any]], identity: Mapping[str, Any], qa_cost: float
) -> tuple[TaskOpenTotals, TaskOpenVersionSummary | None, tuple[int, int]]:
    """Return task totals, selected-version rollups, and current status counts."""
    totals = TaskOpenTotals(qa_cost_usd=qa_cost)
    selected = None
    if identity["selected_version_id"] is not None:
        selected = TaskOpenVersionSummary(
            id=str(identity["selected_version_id"]),
            version=int(identity["selected_version"]),
            message=identity["selected_version_message"],
            created_at=identity["selected_version_created_at"],
            is_current=identity["selected_version_id"]
            == identity["current_version_id"],
        )

    current_total = current_terminal = 0
    agents: dict[tuple[bool, str, str | None], TaskOpenAgentModelSummary] = {}
    for row in rows:
        count = int(row["total"] or 0)
        totals.total_trials += count
        totals.token_count += int(row["token_count"] or 0)
        totals.token_trial_count += int(row["token_trial_count"] or 0)
        _add_cost(totals, row, billed=False)
        if row["is_billed"]:
            _add_cost(totals, row, billed=True)
        if row["is_current"] and not row["is_probe"]:
            current_total += count
            current_terminal += sum(
                int(row[field] or 0) for field in ("completed", "failed", "skipped")
            )
        if selected is None or not row["is_selected"]:
            continue

        _add_counts(selected, row)
        _add_cost(selected, row, billed=False)
        if row["is_billed"]:
            _add_cost(selected, row, billed=True)

        key = _agent_key(row)
        group = agents.setdefault(
            key,
            TaskOpenAgentModelSummary(agent=key[1], model=key[2], is_probe=key[0]),
        )
        provider = row.get("provider")
        if provider and provider not in group.providers:
            group.providers.append(str(provider))
        _add_counts(group, row)
        group.duration_sum_seconds += float(row.get("duration_sum_seconds") or 0.0)
        group.duration_trial_count += int(row.get("duration_trial_count") or 0)
        _add_cost(group, row, billed=False)
        if row["is_billed"]:
            _add_cost(group, row, billed=True)

    if selected is not None:
        _finalize(selected)
        for group in agents.values():
            _finalize(group)
            group.providers.sort()
        selected.agent_models = sorted(
            agents.values(),
            key=lambda item: (item.is_probe, item.agent, item.model or ""),
        )
    return totals, selected, (current_total, current_terminal)
