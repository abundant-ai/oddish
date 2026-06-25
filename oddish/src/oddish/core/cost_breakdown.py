"""Finest-grain trial cost rows for the org-scoped /cost dashboard page.

This powers the dedicated top-level Cost page, which lets a user break spend
down by any one or two of {model, experiment, user} over a bounded window. We
compute the *finest* grain once -- one row per (model x experiment, with the
experiment owner carried along) -- and let the frontend re-aggregate
client-side for every group-by / layout toggle. A ``GROUP BY`` scans the same
windowed rows regardless of grouping granularity, and an experiment has exactly
one owner, so the finest grain costs the database no more than a coarse one.

Cost model -- IMPORTANT: this mirrors the admin cost dashboard
(``oddish.core.admin.get_cost_breakdown_core``): spend is the native runtime
``cost_usd`` when the runtime reported one, plus a per-model token estimate
(``estimate_cost_usd``) for trials where ``cost_usd`` is NULL. The two code
paths are intentionally kept separate (admin is global, this is org-scoped),
but the per-row cost arithmetic MUST stay in sync so the /cost page and the
Admin -> Costs tab never report different totals for the same trials.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import normalize_model_id
from oddish.db import ExperimentModel, TrialModel, TrialStatus
from oddish.model_pricing import estimate_cost_usd

# Safety valve: a bounded time window keeps the windowed scan small, but a busy
# org could still emit many (model x experiment) groups. Past this many groups
# we return the top-N by cost plus a single aggregated ``(other)`` row so the
# payload (and the client re-aggregation) stays bounded; totals still reconcile.
MAX_GROUPS = 5000

# Short in-process cache so rapid time-window toggling and concurrent viewers
# don't each re-scan the trials table. Keyed by (org_id, usage_minutes).
_CACHE_TTL_SECONDS = 10.0
_cache: dict[tuple[str | None, int | None], tuple[float, list[dict[str, Any]]]] = {}

_UNKNOWN_MODEL = "unknown"


def _cache_get(key: tuple[str | None, int | None]) -> list[dict[str, Any]] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, rows = entry
    if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return rows


def _cache_set(key: tuple[str | None, int | None], rows: list[dict[str, Any]]) -> None:
    _cache[key] = (time.monotonic(), rows)


async def get_cost_breakdown_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    usage_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate trial cost/tokens at ``model x experiment`` grain for one org.

    Each returned row carries the experiment's ``owner_user_id`` so the frontend
    can roll spend up by user without a second query. The router layer resolves
    that id to a display label (the ``users`` table lives in the backend
    package, not here). ``cost_usd`` is native + estimated (see module docstring)
    and ``cost_estimated_usd`` is the estimated portion.

    Callers should always pass a finite, bounded ``usage_minutes`` for this view.
    """
    cache_key = (org_id, usage_minutes)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    filters = []
    if org_id is not None:
        filters.append(TrialModel.org_id == org_id)
    if usage_minutes is not None:
        since = datetime.now(timezone.utc) - timedelta(minutes=usage_minutes)
        filters.append(TrialModel.created_at >= since)

    # Native cost is summed in SQL; estimated cost is derived per row in Python
    # (it needs the model name for pricing), so we also sum the token counts of
    # the NULL-cost trials separately for the estimate.
    query = (
        select(
            TrialModel.model,
            TrialModel.provider,
            TrialModel.experiment_id,
            ExperimentModel.name.label("experiment_name"),
            ExperimentModel.owner_user_id,
            func.count(TrialModel.id).label("trial_count"),
            func.coalesce(func.sum(TrialModel.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(TrialModel.cache_tokens), 0).label("cache_tokens"),
            func.coalesce(func.sum(TrialModel.output_tokens), 0).label("output_tokens"),
            func.coalesce(
                func.sum(
                    case((TrialModel.cost_usd.isnot(None), TrialModel.cost_usd), else_=0.0)
                ),
                0.0,
            ).label("native_cost"),
            func.coalesce(
                func.sum(
                    case((TrialModel.cost_usd.is_(None), TrialModel.input_tokens), else_=0)
                ),
                0,
            ).label("est_input"),
            func.coalesce(
                func.sum(
                    case((TrialModel.cost_usd.is_(None), TrialModel.output_tokens), else_=0)
                ),
                0,
            ).label("est_output"),
            func.coalesce(
                func.sum(
                    case((TrialModel.cost_usd.is_(None), TrialModel.cache_tokens), else_=0)
                ),
                0,
            ).label("est_cache"),
            func.count(case((TrialModel.status == TrialStatus.RUNNING, 1))).label(
                "running"
            ),
            func.count(case((TrialModel.status == TrialStatus.RETRYING, 1))).label(
                "retrying"
            ),
            func.count(
                case(
                    (
                        TrialModel.status.in_([TrialStatus.PENDING, TrialStatus.QUEUED]),
                        1,
                    )
                )
            ).label("queued"),
        )
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
        .group_by(
            TrialModel.model,
            TrialModel.provider,
            TrialModel.experiment_id,
            ExperimentModel.name,
            ExperimentModel.owner_user_id,
        )
    )
    if filters:
        query = query.where(*filters)

    result = await session.execute(query)

    # Normalize the model label and re-merge any rows that collapse to the same
    # (model, provider, experiment) after normalization. The estimated cost is
    # computed per raw row (using the raw model for pricing) then accumulated,
    # matching the admin path.
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in result.all():
        provider = (row.provider or "unknown").strip().lower() or "unknown"
        model = normalize_model_id(row.model) or _UNKNOWN_MODEL
        native = float(row.native_cost or 0.0)
        estimated = (
            estimate_cost_usd(
                row.model,
                int(row.est_input or 0),
                int(row.est_output or 0),
                int(row.est_cache or 0),
            )
            or 0.0
        )
        key = (model, provider, row.experiment_id)
        agg = merged.get(key)
        if agg is None:
            agg = {
                "model": model,
                "provider": provider,
                "experiment_id": row.experiment_id,
                "experiment_name": row.experiment_name or row.experiment_id,
                "owner_user_id": row.owner_user_id,
                "cost_usd": 0.0,
                "cost_estimated_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_tokens": 0,
                "trial_count": 0,
                "running": 0,
                "queued": 0,
                "retrying": 0,
            }
            merged[key] = agg
        agg["cost_usd"] += native + estimated
        agg["cost_estimated_usd"] += estimated
        agg["input_tokens"] += int(row.input_tokens or 0)
        agg["output_tokens"] += int(row.output_tokens or 0)
        agg["cache_tokens"] += int(row.cache_tokens or 0)
        agg["trial_count"] += int(row.trial_count or 0)
        agg["running"] += int(row.running or 0)
        agg["queued"] += int(row.queued or 0)
        agg["retrying"] += int(row.retrying or 0)

    rows = list(merged.values())
    for agg in rows:
        agg["cost_usd"] = round(float(agg["cost_usd"]), 4)
        agg["cost_estimated_usd"] = round(float(agg["cost_estimated_usd"]), 4)

    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    rows = _apply_group_cap(rows)

    _cache_set(cache_key, rows)
    return rows


def _apply_group_cap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound the payload: keep the top ``MAX_GROUPS`` rows by cost and fold the
    remainder into a single ``(other)`` row so totals still reconcile."""
    if len(rows) <= MAX_GROUPS:
        return rows

    head = rows[:MAX_GROUPS]
    tail = rows[MAX_GROUPS:]
    other = {
        "model": "(other)",
        "provider": "other",
        "experiment_id": None,
        "experiment_name": "(other)",
        "owner_user_id": None,
        "cost_usd": round(sum(float(r["cost_usd"]) for r in tail), 4),
        "cost_estimated_usd": round(sum(float(r["cost_estimated_usd"]) for r in tail), 4),
        "input_tokens": sum(int(r["input_tokens"]) for r in tail),
        "output_tokens": sum(int(r["output_tokens"]) for r in tail),
        "cache_tokens": sum(int(r["cache_tokens"]) for r in tail),
        "trial_count": sum(int(r["trial_count"]) for r in tail),
        "running": sum(int(r["running"]) for r in tail),
        "queued": sum(int(r["queued"]) for r in tail),
        "retrying": sum(int(r["retrying"]) for r in tail),
        "is_other": True,
    }
    head.append(other)
    return head
