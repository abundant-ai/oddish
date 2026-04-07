from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, exists, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.api.helpers import build_task_status_responses_from_counts
from oddish.config import normalize_model_id
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
)
from oddish.queue import get_pipeline_stats, get_queue_stats_with_concurrency


def _parse_github_meta(raw_github_meta: str | None) -> dict[str, Any] | None:
    if not raw_github_meta:
        return None
    try:
        parsed = json.loads(raw_github_meta)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


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


# ---------------------------------------------------------------------------
# Response Caching
# ---------------------------------------------------------------------------

_dashboard_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 10
_CACHE_MAX_SIZE = 100


def _get_cached(cache_key: str) -> dict | None:
    if cache_key not in _dashboard_cache:
        return None
    cached, cached_at = _dashboard_cache[cache_key]
    if time.time() - cached_at > _CACHE_TTL_SECONDS:
        del _dashboard_cache[cache_key]
        return None
    return cached


def _set_cached(cache_key: str, data: dict) -> None:
    if len(_dashboard_cache) >= _CACHE_MAX_SIZE:
        sorted_keys = sorted(
            _dashboard_cache.keys(), key=lambda k: _dashboard_cache[k][1]
        )
        for k in sorted_keys[: _CACHE_MAX_SIZE // 4]:
            del _dashboard_cache[k]
    _dashboard_cache[cache_key] = (data, time.time())


# ---------------------------------------------------------------------------
# Experiment aggregation
# ---------------------------------------------------------------------------


async def _load_trial_aggregates_for_experiments(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    experiment_ids: list[str],
) -> dict[str, dict[str, int]]:
    """Aggregate trial counts per experiment.

    Uses ``trial.experiment_id`` so that trials attached to a different
    experiment than their task's ``experiment_id`` are counted correctly.
    """
    if not experiment_ids:
        return {}

    filters = [TrialModel.experiment_id.in_(experiment_ids)]
    if org_id is not None:
        filters.append(TrialModel.org_id == org_id)

    result = await session.execute(
        select(
            TrialModel.experiment_id.label("experiment_id"),
            func.count(TrialModel.id).label("total_trials"),
            func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
                "completed_trials"
            ),
            func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
                "failed_trials"
            ),
            func.count(case((TrialModel.reward == 1, 1))).label("reward_success"),
            func.count(case((TrialModel.reward.isnot(None), 1))).label("reward_total"),
        )
        .where(*filters)
        .group_by(TrialModel.experiment_id)
    )

    return {
        str(row.experiment_id): {
            "total_trials": int(row.total_trials or 0),
            "completed_trials": int(row.completed_trials or 0),
            "failed_trials": int(row.failed_trials or 0),
            "reward_success": int(row.reward_success or 0),
            "reward_total": int(row.reward_total or 0),
        }
        for row in result.all()
    }


async def load_dashboard_experiments(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    experiments_limit: int,
    experiments_offset: int,
    experiments_query: str | None,
    experiments_status: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Page experiment rows first, then aggregate trials for the visible page."""

    # Task-level aggregation (via task.experiment_id)
    task_agg_query = select(
        TaskModel.experiment_id.label("experiment_id"),
        func.count(TaskModel.id).label("task_count"),
        func.count(case((TaskModel.run_analysis.is_(True), 1))).label("analysis_tasks"),
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
        func.count(case((TaskModel.verdict_status == VerdictStatus.FAILED, 1))).label(
            "verdict_failed"
        ),
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
        func.max(TaskModel.created_at).label("last_task_created_at"),
    ).where(TaskModel.experiment_id.isnot(None))
    if org_id is not None:
        task_agg_query = task_agg_query.where(TaskModel.org_id == org_id)
    task_agg = task_agg_query.group_by(TaskModel.experiment_id).subquery()

    # Trial-level aggregation (via trial.experiment_id)
    trial_agg_query = select(
        TrialModel.experiment_id.label("experiment_id"),
        func.max(TrialModel.created_at).label("last_trial_created_at"),
        func.count(func.distinct(TrialModel.task_id)).label("trial_task_count"),
    ).where(TrialModel.experiment_id.isnot(None))
    if org_id is not None:
        trial_agg_query = trial_agg_query.where(TrialModel.org_id == org_id)
    trial_agg = trial_agg_query.group_by(TrialModel.experiment_id).subquery()

    # Latest task author info (via task.experiment_id)
    latest_task_query = select(
        TaskModel.experiment_id.label("experiment_id"),
        TaskModel.user.label("last_user"),
        TaskModel.tags["github_username"].astext.label("last_github_username"),
        TaskModel.tags["github_meta"].astext.label("last_github_meta"),
    ).where(TaskModel.experiment_id.isnot(None))
    if org_id is not None:
        latest_task_query = latest_task_query.where(TaskModel.org_id == org_id)
    latest_task = (
        latest_task_query.order_by(
            TaskModel.experiment_id.asc(),
            TaskModel.created_at.desc(),
            TaskModel.id.desc(),
        )
        .distinct(TaskModel.experiment_id)
        .subquery()
    )

    # Build experiment rows starting from ExperimentModel
    exp_base = (
        select(
            ExperimentModel.id.label("experiment_id"),
            ExperimentModel.name.label("experiment_name"),
            case((ExperimentModel.is_public.is_(True), 1), else_=0).label(
                "experiment_is_public"
            ),
            func.greatest(
                func.coalesce(task_agg.c.task_count, 0),
                func.coalesce(trial_agg.c.trial_task_count, 0),
            ).label("task_count"),
            func.coalesce(task_agg.c.analysis_tasks, 0).label("analysis_tasks"),
            func.coalesce(task_agg.c.verdict_good, 0).label("verdict_good"),
            func.coalesce(task_agg.c.verdict_needs_review, 0).label(
                "verdict_needs_review"
            ),
            func.coalesce(task_agg.c.verdict_failed, 0).label("verdict_failed"),
            func.coalesce(task_agg.c.verdict_pending, 0).label("verdict_pending"),
            func.greatest(
                task_agg.c.last_task_created_at,
                trial_agg.c.last_trial_created_at,
            ).label("last_created_at"),
            latest_task.c.last_user,
            latest_task.c.last_github_username,
            latest_task.c.last_github_meta,
        )
        .select_from(ExperimentModel)
        .outerjoin(task_agg, task_agg.c.experiment_id == ExperimentModel.id)
        .outerjoin(trial_agg, trial_agg.c.experiment_id == ExperimentModel.id)
        .outerjoin(latest_task, latest_task.c.experiment_id == ExperimentModel.id)
    )
    exp_filter = or_(
        task_agg.c.experiment_id.isnot(None),
        trial_agg.c.experiment_id.isnot(None),
    )
    if org_id is not None:
        exp_filter = and_(exp_filter, ExperimentModel.org_id == org_id)
    experiment_rows = exp_base.where(exp_filter).subquery()

    # Status filter helpers (use trial.experiment_id for correctness)
    active_trial_filters = [
        TrialModel.experiment_id == experiment_rows.c.experiment_id,
        TrialModel.status.in_(
            [
                TrialStatus.PENDING,
                TrialStatus.QUEUED,
                TrialStatus.RUNNING,
                TrialStatus.RETRYING,
            ]
        ),
    ]
    if org_id is not None:
        active_trial_filters.append(TrialModel.org_id == org_id)
    active_trial_exists = exists(
        select(1).select_from(TrialModel).where(*active_trial_filters)
    )

    failed_trial_filters = [
        TrialModel.experiment_id == experiment_rows.c.experiment_id,
        TrialModel.status == TrialStatus.FAILED,
    ]
    if org_id is not None:
        failed_trial_filters.append(TrialModel.org_id == org_id)
    failed_trial_exists = exists(
        select(1).select_from(TrialModel).where(*failed_trial_filters)
    )

    query = select(experiment_rows)

    normalized_query = (experiments_query or "").strip().lower()
    if normalized_query:
        query_like = f"%{normalized_query}%"
        query = query.where(
            or_(
                func.lower(experiment_rows.c.experiment_name).like(query_like),
                func.lower(experiment_rows.c.experiment_id).like(query_like),
                func.lower(func.coalesce(experiment_rows.c.last_user, "")).like(
                    query_like
                ),
                func.lower(
                    func.coalesce(experiment_rows.c.last_github_username, "")
                ).like(query_like),
            )
        )

    if experiments_status == "active":
        query = query.where(active_trial_exists)
    elif experiments_status == "needs-review":
        query = query.where(experiment_rows.c.verdict_needs_review > 0)
    elif experiments_status == "pending-verdict":
        query = query.where(experiment_rows.c.verdict_pending > 0)
    elif experiments_status == "failed":
        query = query.where(
            or_(experiment_rows.c.verdict_failed > 0, failed_trial_exists)
        )
    elif experiments_status == "completed":
        query = query.where(~active_trial_exists)

    paged_rows = (
        (
            await session.execute(
                query.order_by(
                    nulls_last(experiment_rows.c.last_created_at.desc()),
                    experiment_rows.c.experiment_id.asc(),
                )
                .limit(experiments_limit + 1)
                .offset(experiments_offset)
            )
        )
        .mappings()
        .all()
    )

    experiments_has_more = len(paged_rows) > experiments_limit
    page_rows = paged_rows[:experiments_limit]
    trial_aggregates = await _load_trial_aggregates_for_experiments(
        session,
        org_id=org_id,
        experiment_ids=[str(row["experiment_id"]) for row in page_rows],
    )

    experiments_response: list[dict[str, Any]] = []
    for row in page_rows:
        github_meta = _parse_github_meta(row["last_github_meta"])
        last_author_name = row["last_github_username"] or row["last_user"]
        last_author_source = "github" if row["last_github_username"] else "api"
        trial_counts = trial_aggregates.get(
            str(row["experiment_id"]),
            {
                "total_trials": 0,
                "completed_trials": 0,
                "failed_trials": 0,
                "reward_success": 0,
                "reward_total": 0,
            },
        )
        total_trials = int(trial_counts["total_trials"])
        completed_trials = int(trial_counts["completed_trials"])
        failed_trials = int(trial_counts["failed_trials"])

        experiments_response.append(
            {
                "id": row["experiment_id"],
                "name": row["experiment_name"],
                "is_public": bool(row["experiment_is_public"]),
                "task_count": int(row["task_count"] or 0),
                "total_trials": total_trials,
                "completed_trials": completed_trials,
                "failed_trials": failed_trials,
                "active_trials": max(
                    0, total_trials - completed_trials - failed_trials
                ),
                "reward_success": int(trial_counts["reward_success"]),
                "reward_total": int(trial_counts["reward_total"]),
                "analysis_tasks": int(row["analysis_tasks"] or 0),
                "verdict_good": int(row["verdict_good"] or 0),
                "verdict_needs_review": int(row["verdict_needs_review"] or 0),
                "verdict_failed": int(row["verdict_failed"] or 0),
                "verdict_pending": int(row["verdict_pending"] or 0),
                "last_created_at": (
                    row["last_created_at"].isoformat()
                    if row["last_created_at"]
                    else None
                ),
                "last_author": (
                    {"name": last_author_name, "source": last_author_source}
                    if last_author_name
                    else None
                ),
                "last_pr_url": (
                    str(github_meta["pr_url"])
                    if github_meta and github_meta.get("pr_url") is not None
                    else None
                ),
                "last_pr_title": (
                    str(github_meta["pr_title"])
                    if github_meta and github_meta.get("pr_title") is not None
                    else None
                ),
                "last_pr_number": (
                    str(github_meta["pr_number"])
                    if github_meta and github_meta.get("pr_number") is not None
                    else None
                ),
            }
        )

    return experiments_response, experiments_has_more


# ---------------------------------------------------------------------------
# Model usage aggregation
# ---------------------------------------------------------------------------


async def get_model_usage_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    usage_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate per-model cost and token usage from trials."""
    usage_filters = []
    if org_id is not None:
        usage_filters.append(TrialModel.org_id == org_id)
    if usage_minutes is not None:
        since = datetime.now(timezone.utc) - timedelta(minutes=usage_minutes)
        usage_filters.append(TrialModel.created_at >= since)

    usage_query = select(
        TrialModel.model,
        TrialModel.provider,
        func.count(TrialModel.id).label("trial_count"),
        func.sum(TrialModel.input_tokens).label("input_tokens"),
        func.sum(TrialModel.cache_tokens).label("cache_tokens"),
        func.sum(TrialModel.output_tokens).label("output_tokens"),
        func.sum(TrialModel.cost_usd).label("cost_usd"),
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
        func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
            "succeeded"
        ),
        func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label("failed"),
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
    ).group_by(TrialModel.model, TrialModel.provider)
    if usage_filters:
        usage_query = usage_query.where(*usage_filters)

    usage_result = await session.execute(usage_query)
    merged: dict[tuple[str, str], dict[str, int | float | str | None]] = {}
    for row in usage_result.all():
        normalized_provider = (row.provider or "unknown").strip().lower() or "unknown"
        normalized_model = _normalize_dashboard_model(row.model, normalized_provider)
        key = (normalized_model, normalized_provider)
        duration_count = int(row.duration_count or 0)

        if key not in merged:
            merged[key] = {
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

        agg = merged[key]
        agg["trial_count"] = int(agg["trial_count"]) + int(row.trial_count or 0)
        agg["input_tokens"] = int(agg["input_tokens"]) + int(row.input_tokens or 0)
        agg["cache_tokens"] = int(agg["cache_tokens"]) + int(row.cache_tokens or 0)
        agg["output_tokens"] = int(agg["output_tokens"]) + int(row.output_tokens or 0)
        agg["cost_usd"] = float(agg["cost_usd"]) + float(row.cost_usd or 0)
        agg["running"] = int(agg["running"]) + int(row.running or 0)
        agg["retrying"] = int(agg["retrying"]) + int(row.retrying or 0)
        agg["queued"] = int(agg["queued"]) + int(row.queued or 0)
        agg["succeeded"] = int(agg["succeeded"]) + int(row.succeeded or 0)
        agg["failed"] = int(agg["failed"]) + int(row.failed or 0)
        agg["duration_total_s"] = float(agg["duration_total_s"]) + float(
            (row.avg_duration_s or 0) * duration_count
        )
        agg["duration_count"] = int(agg["duration_count"]) + duration_count

    model_usage: list[dict[str, Any]] = []
    for agg in merged.values():
        dc = int(agg["duration_count"])
        avg_dur = round(float(agg["duration_total_s"]) / dc, 1) if dc > 0 else None
        model_usage.append(
            {
                "model": str(agg["model"]),
                "provider": str(agg["provider"]),
                "trial_count": int(agg["trial_count"]),
                "input_tokens": int(agg["input_tokens"]),
                "cache_tokens": int(agg["cache_tokens"]),
                "output_tokens": int(agg["output_tokens"]),
                "cost_usd": round(float(agg["cost_usd"]), 4),
                "running": int(agg["running"]),
                "retrying": int(agg["retrying"]),
                "queued": int(agg["queued"]),
                "succeeded": int(agg["succeeded"]),
                "failed": int(agg["failed"]),
                "avg_duration_s": avg_dur,
            }
        )
    return model_usage


# ---------------------------------------------------------------------------
# Full dashboard core
# ---------------------------------------------------------------------------


async def get_dashboard_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    tasks_limit: int = 200,
    tasks_offset: int = 0,
    experiments_limit: int = 25,
    experiments_offset: int = 0,
    experiments_query: str | None = None,
    experiments_status: str = "all",
    usage_minutes: int | None = None,
    include_tasks: bool = True,
    include_usage: bool = True,
    include_experiments: bool = True,
) -> dict:
    """Combined dashboard data: queues, pipeline, usage, tasks, experiments.

    When experiments are requested alongside other data, the experiment query
    runs on a separate DB session in parallel (max 2 connections) to cut
    overall latency significantly.
    """

    cache_key = (
        f"dashboard:{org_id}:{tasks_limit}:{tasks_offset}:"
        f"{experiments_limit}:{experiments_offset}:{experiments_query}:"
        f"{experiments_status}:{usage_minutes}:{include_tasks}:{include_usage}:"
        f"{include_experiments}"
    )
    cached = _get_cached(cache_key)
    if cached:
        return cached

    is_usage_only_request = (
        include_usage and not include_tasks and not include_experiments
    )

    async def _fetch_primary() -> (
        tuple[dict, dict[str, dict[str, int]], list[dict[str, Any]], list[dict], bool]
    ):
        """Queue stats, pipeline stats, usage, and tasks on the caller's session."""
        if is_usage_only_request:
            qs: dict = {}
            ps: dict[str, dict[str, int]] = {
                "trials": {},
                "analyses": {},
                "verdicts": {},
            }
        else:
            qs = await get_queue_stats_with_concurrency(session, org_id)
            ps = await get_pipeline_stats(session, org_id)

        mu: list[dict[str, Any]] = []
        if include_usage:
            mu = await get_model_usage_core(
                session, org_id=org_id, usage_minutes=usage_minutes
            )

        tr: list[dict] = []
        hm = False
        if include_tasks:
            tasks_q = (
                select(TaskModel)
                .options(selectinload(TaskModel.experiment))
                .order_by(TaskModel.created_at.desc())
                .limit(tasks_limit + 1)
                .offset(tasks_offset)
            )
            if org_id is not None:
                tasks_q = tasks_q.where(TaskModel.org_id == org_id)

            tasks_result = await session.execute(tasks_q)
            paged_tasks = tasks_result.scalars().all()
            hm = len(paged_tasks) > tasks_limit
            fetched_tasks = paged_tasks[:tasks_limit]

            if fetched_tasks:
                tr = [
                    ts.model_dump()
                    for ts in await build_task_status_responses_from_counts(
                        session, tasks=fetched_tasks
                    )
                ]

        return qs, ps, mu, tr, hm

    async def _fetch_experiments_parallel() -> tuple[list[dict[str, Any]], bool]:
        """Experiments on a separate session so they run concurrently."""
        async with get_session() as exp_session:
            return await load_dashboard_experiments(
                exp_session,
                org_id=org_id,
                experiments_limit=experiments_limit,
                experiments_offset=experiments_offset,
                experiments_query=experiments_query,
                experiments_status=experiments_status,
            )

    if include_experiments:
        # Run experiments on a separate session in parallel with primary queries.
        (queue_stats, pipeline_stats, model_usage, tasks_response, has_more), (
            experiments_response,
            experiments_has_more,
        ) = await asyncio.gather(
            _fetch_primary(), _fetch_experiments_parallel()
        )
    else:
        queue_stats, pipeline_stats, model_usage, tasks_response, has_more = (
            await _fetch_primary()
        )
        experiments_response = []
        experiments_has_more = False

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

    _set_cached(cache_key, {**response, "cached": True})
    return response
