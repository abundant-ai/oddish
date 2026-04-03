from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload

from ..dashboard_experiments import load_dashboard_experiments
from oddish.api.helpers import build_task_status_responses_from_counts
from oddish.config import normalize_model_id
from auth import APIKeyScope, AuthContext, require_auth
from oddish.db import (
    TaskModel,
    TrialModel,
    get_session,
)
from oddish.db.models import TrialStatus
from oddish.queue import get_pipeline_stats, get_queue_stats_with_concurrency

router = APIRouter(tags=["Dashboard"])


# =============================================================================
# Response Caching
# =============================================================================
# Simple in-memory cache for dashboard responses.
# Key: (org_id, endpoint_params_hash)
# Value: (response_data, timestamp)

_dashboard_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 10  # Short TTL - data freshness vs performance tradeoff
_CACHE_MAX_SIZE = 100


def _normalize_dashboard_model(model: str | None, provider: str | None) -> str:
    """Preserve the nop/oracle default model label in usage tables."""
    normalized_model = normalize_model_id(model)
    if normalized_model:
        return normalized_model

    normalized_provider = (provider or "").strip().lower()
    raw_model = (model or "").strip().lower()
    if raw_model == "default" or normalized_provider == "default":
        return "default"

    return "unknown"


def _get_cached(cache_key: str) -> dict | None:
    """Get cached response if still valid."""
    if cache_key not in _dashboard_cache:
        return None
    cached, cached_at = _dashboard_cache[cache_key]
    if time.time() - cached_at > _CACHE_TTL_SECONDS:
        del _dashboard_cache[cache_key]
        return None
    return cached


def _set_cached(cache_key: str, data: dict) -> None:
    """Cache response with current timestamp."""
    # Simple size limit
    if len(_dashboard_cache) >= _CACHE_MAX_SIZE:
        # Remove oldest entries
        sorted_keys = sorted(
            _dashboard_cache.keys(), key=lambda k: _dashboard_cache[k][1]
        )
        for k in sorted_keys[: _CACHE_MAX_SIZE // 4]:
            del _dashboard_cache[k]
    _dashboard_cache[cache_key] = (data, time.time())


# =============================================================================
# Combined Dashboard Endpoint
# =============================================================================


@router.get("/dashboard")
async def get_dashboard(
    auth: Annotated[AuthContext, Depends(require_auth)],
    tasks_limit: int = Query(200, ge=1, le=500),
    tasks_offset: int = Query(0, ge=0),
    experiments_limit: int = Query(25, ge=1, le=100),
    experiments_offset: int = Query(0, ge=0),
    experiments_query: str | None = Query(None),
    experiments_status: str = Query("all"),
    usage_minutes: int | None = Query(None, ge=1, le=86400),
    include_tasks: bool = Query(True),
    include_usage: bool = Query(True),
    include_experiments: bool = Query(True),
) -> dict:
    """
    Combined dashboard endpoint returning queues and recent tasks.

    This eliminates 3 separate API calls, reducing latency significantly.
    Response is cached for 10 seconds per organization.

    Args:
        usage_minutes: Time window for model_usage aggregation (e.g. 60 = last hour).
                       Omit for all-time totals.
    """
    auth.require_scope(APIKeyScope.READ)

    # Check cache first
    cache_key = (
        f"dashboard:{auth.org_id}:{tasks_limit}:{tasks_offset}:"
        f"{experiments_limit}:{experiments_offset}:{experiments_query}:"
        f"{experiments_status}:{usage_minutes}:{include_tasks}:{include_usage}:"
        f"{include_experiments}"
    )
    cached = _get_cached(cache_key)
    if cached:
        return cached

    async with get_session() as session:
        # =====================================================================
        # 1. Queue/Pipeline Stats
        # =====================================================================
        # Usage-only requests back the dashboard usage card and do not render
        # queue/pipeline sections. Skip these expensive aggregations to reduce
        # time-window switch latency.
        is_usage_only_request = (
            include_usage and not include_tasks and not include_experiments
        )
        if is_usage_only_request:
            queue_stats = {}
            pipeline_stats: dict[str, dict[str, int]] = {
                "trials": {},
                "analyses": {},
                "verdicts": {},
            }
        else:
            queue_stats = await get_queue_stats_with_concurrency(session, auth.org_id)
            pipeline_stats = await get_pipeline_stats(session, auth.org_id)

        # =====================================================================
        # 2. Per-model cost & token usage (aggregated from trials)
        # =====================================================================
        model_usage: list[dict[str, Any]] = []
        if include_usage:
            usage_filters = [TrialModel.org_id == auth.org_id]
            if usage_minutes is not None:
                since = datetime.now(timezone.utc) - timedelta(minutes=usage_minutes)
                usage_filters.append(TrialModel.created_at >= since)

            usage_query = (
                select(
                    TrialModel.model,
                    TrialModel.provider,
                    func.count(TrialModel.id).label("trial_count"),
                    func.sum(TrialModel.input_tokens).label("input_tokens"),
                    func.sum(TrialModel.cache_tokens).label("cache_tokens"),
                    func.sum(TrialModel.output_tokens).label("output_tokens"),
                    func.sum(TrialModel.cost_usd).label("cost_usd"),
                    func.count(
                        case((TrialModel.status == TrialStatus.RUNNING, 1))
                    ).label("running"),
                    func.count(
                        case((TrialModel.status == TrialStatus.RETRYING, 1))
                    ).label("retrying"),
                    func.count(
                        case(
                            (
                                TrialModel.status.in_(
                                    [
                                        TrialStatus.PENDING,
                                        TrialStatus.QUEUED,
                                    ]
                                ),
                                1,
                            )
                        )
                    ).label("queued"),
                    func.count(
                        case((TrialModel.status == TrialStatus.SUCCESS, 1))
                    ).label("succeeded"),
                    func.count(
                        case((TrialModel.status == TrialStatus.FAILED, 1))
                    ).label("failed"),
                    func.avg(
                        case(
                            (
                                TrialModel.finished_at.isnot(None),
                                func.extract(
                                    "epoch",
                                    TrialModel.finished_at - TrialModel.started_at,
                                ),
                            )
                        )
                    ).label("avg_duration_s"),
                    func.count(case((TrialModel.finished_at.isnot(None), 1))).label(
                        "duration_count"
                    ),
                )
                .where(*usage_filters)
                .group_by(TrialModel.model, TrialModel.provider)
            )

            usage_result = await session.execute(usage_query)
            merged_usage: dict[tuple[str, str], dict[str, int | float | str | None]] = (
                {}
            )
            for row in usage_result.all():
                normalized_provider = (
                    row.provider or "unknown"
                ).strip().lower() or "unknown"
                normalized_model = _normalize_dashboard_model(
                    row.model, normalized_provider
                )
                key = (normalized_model, normalized_provider)
                duration_count = int(row.duration_count or 0)

                if key not in merged_usage:
                    merged_usage[key] = {
                        "model": normalized_model,
                        "provider": normalized_provider,
                        "trial_count": 0,
                        "input_tokens": 0,
                        "cache_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "running": 0,
                        "retrying": 0,
                        "queued": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "duration_total_s": 0.0,
                        "duration_count": 0,
                        "avg_duration_s": None,
                    }

                aggregate = merged_usage[key]
                aggregate["trial_count"] = int(aggregate["trial_count"]) + int(
                    row.trial_count or 0
                )
                aggregate["input_tokens"] = int(aggregate["input_tokens"]) + int(
                    row.input_tokens or 0
                )
                aggregate["cache_tokens"] = int(aggregate["cache_tokens"]) + int(
                    row.cache_tokens or 0
                )
                aggregate["output_tokens"] = int(aggregate["output_tokens"]) + int(
                    row.output_tokens or 0
                )
                aggregate["cost_usd"] = float(aggregate["cost_usd"]) + float(
                    row.cost_usd or 0
                )
                aggregate["running"] = int(aggregate["running"]) + int(row.running or 0)
                aggregate["retrying"] = int(aggregate["retrying"]) + int(
                    row.retrying or 0
                )
                aggregate["queued"] = int(aggregate["queued"]) + int(row.queued or 0)
                aggregate["succeeded"] = int(aggregate["succeeded"]) + int(
                    row.succeeded or 0
                )
                aggregate["failed"] = int(aggregate["failed"]) + int(row.failed or 0)
                aggregate["duration_total_s"] = float(
                    aggregate["duration_total_s"]
                ) + float((row.avg_duration_s or 0) * duration_count)
                aggregate["duration_count"] = (
                    int(aggregate["duration_count"]) + duration_count
                )

            model_usage = []
            for aggregate in merged_usage.values():
                duration_count = int(aggregate["duration_count"])
                avg_duration_s = (
                    round(float(aggregate["duration_total_s"]) / duration_count, 1)
                    if duration_count > 0
                    else None
                )
                model_usage.append(
                    {
                        "model": str(aggregate["model"]),
                        "provider": str(aggregate["provider"]),
                        "trial_count": int(aggregate["trial_count"]),
                        "input_tokens": int(aggregate["input_tokens"]),
                        "cache_tokens": int(aggregate["cache_tokens"]),
                        "output_tokens": int(aggregate["output_tokens"]),
                        "cost_usd": round(float(aggregate["cost_usd"]), 4),
                        "running": int(aggregate["running"]),
                        "retrying": int(aggregate["retrying"]),
                        "queued": int(aggregate["queued"]),
                        "succeeded": int(aggregate["succeeded"]),
                        "failed": int(aggregate["failed"]),
                        "avg_duration_s": avg_duration_s,
                    }
                )

        # =====================================================================
        # 3. Recent Tasks (optimized two-phase query)
        # =====================================================================
        # Phase 1: Fetch paginated tasks
        tasks_response = []
        has_more = False
        if include_tasks:
            tasks_query = (
                select(TaskModel)
                .options(selectinload(TaskModel.experiment))
                .where(TaskModel.org_id == auth.org_id)
                .order_by(TaskModel.created_at.desc())
                .limit(tasks_limit + 1)
                .offset(tasks_offset)
            )

            tasks_result = await session.execute(tasks_query)
            paged_tasks = tasks_result.scalars().all()
            has_more = len(paged_tasks) > tasks_limit
            tasks = paged_tasks[:tasks_limit]

            if tasks:
                tasks_response = [
                    task_status.model_dump()
                    for task_status in await build_task_status_responses_from_counts(
                        session, tasks=tasks
                    )
                ]

        # =====================================================================
        # 4. Experiment table data (server-side pagination + search)
        # =====================================================================
        experiments_response = []
        experiments_has_more = False
        if include_experiments:
            experiments_response, experiments_has_more = await load_dashboard_experiments(
                session,
                org_id=auth.org_id,
                experiments_limit=experiments_limit,
                experiments_offset=experiments_offset,
                experiments_query=experiments_query,
                experiments_status=experiments_status,
            )

    response = {
        "queues": queue_stats,
        "pipeline": pipeline_stats,
        "model_usage": model_usage,
        "tasks": tasks_response,
        "tasks_limit": tasks_limit,
        "tasks_offset": tasks_offset,
        "has_more": has_more,
        "experiments": experiments_response,
        "experiments_limit": experiments_limit,
        "experiments_offset": experiments_offset,
        "experiments_has_more": experiments_has_more,
        "cached": False,
    }

    # Cache the response
    _set_cached(cache_key, {**response, "cached": True})

    return response
