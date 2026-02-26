from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import selectinload

from oddish.api.helpers import build_task_status_responses_from_counts
from auth import APIKeyScope, AuthContext, require_auth
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    VerdictStatus,
    get_session,
    utcnow,
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
    usage_minutes: int | None = Query(None, ge=1, le=43200),
    include_tasks: bool = Query(True),
    include_usage: bool = Query(True),
    include_experiments: bool = Query(True),
) -> dict:
    """
    Combined dashboard endpoint returning health, queues, and recent tasks.

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
        # 1. Health Check (inline, no separate query needed)
        # =====================================================================
        try:
            await session.execute(text("SELECT 1"))
            health = {
                "status": "healthy",
                "database": "connected",
                "timestamp": utcnow().isoformat(),
            }
        except Exception:
            health = {
                "status": "degraded",
                "database": "disconnected",
                "timestamp": utcnow().isoformat(),
            }

        # =====================================================================
        # 2. Queue/Pipeline Stats
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
        # 3. Per-model cost & token usage (aggregated from trials)
        # =====================================================================
        model_usage = []
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
                        case(
                            (
                                TrialModel.status.in_(
                                    [
                                        TrialStatus.PENDING,
                                        TrialStatus.QUEUED,
                                        TrialStatus.RETRYING,
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
                )
                .where(*usage_filters)
                .group_by(TrialModel.model, TrialModel.provider)
            )

            usage_result = await session.execute(usage_query)
            model_usage = [
                {
                    "model": row.model or "unknown",
                    "provider": row.provider or "unknown",
                    "trial_count": int(row.trial_count),
                    "input_tokens": int(row.input_tokens or 0),
                    "cache_tokens": int(row.cache_tokens or 0),
                    "output_tokens": int(row.output_tokens or 0),
                    "cost_usd": round(float(row.cost_usd or 0), 4),
                    "running": int(row.running),
                    "queued": int(row.queued),
                    "succeeded": int(row.succeeded),
                    "failed": int(row.failed),
                    "avg_duration_s": (
                        round(float(row.avg_duration_s), 1)
                        if row.avg_duration_s
                        else None
                    ),
                }
                for row in usage_result.all()
            ]

        # =====================================================================
        # 4. Recent Tasks (optimized two-phase query)
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
        # 5. Experiment table data (server-side pagination + search)
        # =====================================================================
        experiments_response = []
        experiments_total = 0
        experiments_has_more = False
        if include_experiments:
            task_agg = (
                select(
                    TaskModel.experiment_id.label("experiment_id"),
                    func.max(ExperimentModel.name).label("experiment_name"),
                    func.max(
                        case((ExperimentModel.is_public.is_(True), 1), else_=0)
                    ).label("experiment_is_public"),
                    func.count(TaskModel.id).label("task_count"),
                    func.count(case((TaskModel.run_analysis.is_(True), 1))).label(
                        "analysis_tasks"
                    ),
                    func.count(
                        case(
                            (
                                and_(
                                    TaskModel.verdict_status == VerdictStatus.SUCCESS,
                                    TaskModel.verdict["is_good"].astext == "true",
                                ),
                                1,
                            )
                        )
                    ).label("verdict_good"),
                    func.count(
                        case(
                            (
                                and_(
                                    TaskModel.verdict_status == VerdictStatus.SUCCESS,
                                    TaskModel.verdict["is_good"].astext == "false",
                                ),
                                1,
                            )
                        )
                    ).label("verdict_needs_review"),
                    func.count(
                        case((TaskModel.verdict_status == VerdictStatus.FAILED, 1))
                    ).label("verdict_failed"),
                    func.count(
                        case(
                            (
                                and_(
                                    TaskModel.run_analysis.is_(True),
                                    or_(
                                        TaskModel.verdict_status.is_(None),
                                        TaskModel.verdict_status.in_(
                                            [
                                                VerdictStatus.PENDING,
                                                VerdictStatus.QUEUED,
                                                VerdictStatus.RUNNING,
                                            ]
                                        ),
                                        TaskModel.status.in_(
                                            [
                                                TaskStatus.ANALYZING,
                                                TaskStatus.VERDICT_PENDING,
                                            ]
                                        ),
                                    ),
                                ),
                                1,
                            )
                        )
                    ).label("verdict_pending"),
                    func.max(TaskModel.created_at).label("last_created_at"),
                )
                .join(ExperimentModel, ExperimentModel.id == TaskModel.experiment_id)
                .where(
                    TaskModel.org_id == auth.org_id,
                    ExperimentModel.org_id == auth.org_id,
                )
                .group_by(TaskModel.experiment_id)
                .subquery()
            )

            trial_agg = (
                select(
                    TaskModel.experiment_id.label("experiment_id"),
                    func.count(TrialModel.id).label("total_trials"),
                    func.count(
                        case((TrialModel.status == TrialStatus.SUCCESS, 1))
                    ).label("completed_trials"),
                    func.count(
                        case((TrialModel.status == TrialStatus.FAILED, 1))
                    ).label("failed_trials"),
                    func.count(case((TrialModel.reward == 1, 1))).label(
                        "reward_success"
                    ),
                    func.count(case((TrialModel.reward.isnot(None), 1))).label(
                        "reward_total"
                    ),
                )
                .join(TaskModel, TaskModel.id == TrialModel.task_id)
                .where(TaskModel.org_id == auth.org_id)
                .group_by(TaskModel.experiment_id)
                .subquery()
            )

            latest_task_ranked = (
                select(
                    TaskModel.experiment_id.label("experiment_id"),
                    TaskModel.user.label("last_user"),
                    TaskModel.tags["github_username"].astext.label(
                        "last_github_username"
                    ),
                    TaskModel.tags["github_meta"].astext.label("last_github_meta"),
                    func.row_number()
                    .over(
                        partition_by=TaskModel.experiment_id,
                        order_by=TaskModel.created_at.desc(),
                    )
                    .label("row_number"),
                )
                .where(TaskModel.org_id == auth.org_id)
                .subquery()
            )
            latest_task = (
                select(
                    latest_task_ranked.c.experiment_id,
                    latest_task_ranked.c.last_user,
                    latest_task_ranked.c.last_github_username,
                    latest_task_ranked.c.last_github_meta,
                )
                .where(latest_task_ranked.c.row_number == 1)
                .subquery()
            )

            experiment_rows = (
                select(
                    task_agg.c.experiment_id,
                    task_agg.c.experiment_name,
                    task_agg.c.experiment_is_public,
                    task_agg.c.task_count,
                    task_agg.c.analysis_tasks,
                    task_agg.c.verdict_good,
                    task_agg.c.verdict_needs_review,
                    task_agg.c.verdict_failed,
                    task_agg.c.verdict_pending,
                    task_agg.c.last_created_at,
                    func.coalesce(trial_agg.c.total_trials, 0).label("total_trials"),
                    func.coalesce(trial_agg.c.completed_trials, 0).label(
                        "completed_trials"
                    ),
                    func.coalesce(trial_agg.c.failed_trials, 0).label("failed_trials"),
                    func.coalesce(trial_agg.c.reward_success, 0).label(
                        "reward_success"
                    ),
                    func.coalesce(trial_agg.c.reward_total, 0).label("reward_total"),
                    latest_task.c.last_user,
                    latest_task.c.last_github_username,
                    latest_task.c.last_github_meta,
                )
                .outerjoin(
                    trial_agg, trial_agg.c.experiment_id == task_agg.c.experiment_id
                )
                .outerjoin(
                    latest_task, latest_task.c.experiment_id == task_agg.c.experiment_id
                )
                .subquery()
            )

            active_trials_expr = (
                experiment_rows.c.total_trials
                - experiment_rows.c.completed_trials
                - experiment_rows.c.failed_trials
            )
            filtered_query = select(experiment_rows)
            filters: list[Any] = []

            normalized_query = (experiments_query or "").strip().lower()
            if normalized_query:
                query_like = f"%{normalized_query}%"
                filters.append(
                    or_(
                        func.lower(experiment_rows.c.experiment_name).like(query_like),
                        func.lower(experiment_rows.c.experiment_id).like(query_like),
                        func.lower(func.coalesce(experiment_rows.c.last_user, "")).like(
                            query_like
                        ),
                        func.lower(
                            func.coalesce(
                                experiment_rows.c.last_github_username,
                                "",
                            )
                        ).like(query_like),
                    )
                )

            if experiments_status == "active":
                filters.append(active_trials_expr > 0)
            elif experiments_status == "needs-review":
                filters.append(experiment_rows.c.verdict_needs_review > 0)
            elif experiments_status == "pending-verdict":
                filters.append(experiment_rows.c.verdict_pending > 0)
            elif experiments_status == "failed":
                filters.append(
                    or_(
                        experiment_rows.c.failed_trials > 0,
                        experiment_rows.c.verdict_failed > 0,
                    )
                )
            elif experiments_status == "completed":
                filters.append(active_trials_expr <= 0)

            if filters:
                filtered_query = filtered_query.where(*filters)

            filtered_subquery = filtered_query.subquery()
            total_result = await session.execute(
                select(func.count()).select_from(filtered_subquery)
            )
            experiments_total = int(total_result.scalar_one() or 0)

            paged_result = await session.execute(
                select(filtered_subquery)
                .order_by(filtered_subquery.c.last_created_at.desc())
                .limit(experiments_limit + 1)
                .offset(experiments_offset)
            )
            paged_rows = paged_result.all()
            experiments_has_more = len(paged_rows) > experiments_limit

            for row in paged_rows[:experiments_limit]:
                github_meta = None
                if row.last_github_meta:
                    try:
                        parsed = json.loads(row.last_github_meta)
                        if isinstance(parsed, dict):
                            github_meta = parsed
                    except (TypeError, json.JSONDecodeError):
                        github_meta = None

                last_author_name = row.last_github_username or row.last_user
                last_author_source = "github" if row.last_github_username else "api"
                last_author = (
                    {"name": last_author_name, "source": last_author_source}
                    if last_author_name
                    else None
                )

                experiments_response.append(
                    {
                        "id": row.experiment_id,
                        "name": row.experiment_name,
                        "is_public": bool(row.experiment_is_public),
                        "task_count": int(row.task_count or 0),
                        "total_trials": int(row.total_trials or 0),
                        "completed_trials": int(row.completed_trials or 0),
                        "failed_trials": int(row.failed_trials or 0),
                        "active_trials": max(
                            0,
                            int(row.total_trials or 0)
                            - int(row.completed_trials or 0)
                            - int(row.failed_trials or 0),
                        ),
                        "reward_success": int(row.reward_success or 0),
                        "reward_total": int(row.reward_total or 0),
                        "analysis_tasks": int(row.analysis_tasks or 0),
                        "verdict_good": int(row.verdict_good or 0),
                        "verdict_needs_review": int(row.verdict_needs_review or 0),
                        "verdict_failed": int(row.verdict_failed or 0),
                        "verdict_pending": int(row.verdict_pending or 0),
                        "last_created_at": (
                            row.last_created_at.isoformat()
                            if row.last_created_at
                            else None
                        ),
                        "last_author": last_author,
                        "last_pr_url": (
                            github_meta.get("pr_url") if github_meta else None
                        ),
                        "last_pr_title": (
                            github_meta.get("pr_title") if github_meta else None
                        ),
                        "last_pr_number": (
                            github_meta.get("pr_number") if github_meta else None
                        ),
                    }
                )

    response = {
        "health": health,
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
        "experiments_total": experiments_total,
        "experiments_has_more": experiments_has_more,
        "cached": False,
    }

    # Cache the response
    _set_cached(cache_key, {**response, "cached": True})

    return response
