from __future__ import annotations

import json
from typing import Any

from sqlalchemy import and_, case, exists, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import ExperimentModel, TaskModel, TaskStatus, TrialModel, VerdictStatus
from oddish.db.models import TrialStatus


def _parse_github_meta(raw_github_meta: str | None) -> dict[str, Any] | None:
    if not raw_github_meta:
        return None
    try:
        parsed = json.loads(raw_github_meta)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _load_trial_aggregates_for_experiments(
    session: AsyncSession,
    *,
    org_id: str,
    experiment_ids: list[str],
) -> dict[str, dict[str, int]]:
    """Aggregate trial counts per experiment.

    Uses ``trial.experiment_id`` so that trials attached to a different
    experiment than their task's ``experiment_id`` are counted correctly.
    """
    if not experiment_ids:
        return {}

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
        .where(
            TrialModel.org_id == org_id,
            TrialModel.experiment_id.in_(experiment_ids),
        )
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
    org_id: str,
    experiments_limit: int,
    experiments_offset: int,
    experiments_query: str | None,
    experiments_status: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Page experiment rows first, then aggregate trials for the visible page.

    Starts from ``ExperimentModel`` and LEFT JOINs to task / trial aggregations
    so that experiments discovered only via ``trial.experiment_id`` (no task
    pointing to them) still appear on the dashboard.
    """

    # -----------------------------------------------------------------
    # Task-level aggregation (via task.experiment_id)
    # -----------------------------------------------------------------
    task_agg = (
        select(
            TaskModel.experiment_id.label("experiment_id"),
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
            func.max(TaskModel.created_at).label("last_task_created_at"),
        )
        .where(
            TaskModel.org_id == org_id,
            TaskModel.experiment_id.isnot(None),
        )
        .group_by(TaskModel.experiment_id)
        .subquery()
    )

    # -----------------------------------------------------------------
    # Trial-level timing (via trial.experiment_id) — used for sorting
    # experiments that only have trial-level associations
    # -----------------------------------------------------------------
    trial_timing = (
        select(
            TrialModel.experiment_id.label("experiment_id"),
            func.max(TrialModel.created_at).label("last_trial_created_at"),
        )
        .where(
            TrialModel.org_id == org_id,
            TrialModel.experiment_id.isnot(None),
        )
        .group_by(TrialModel.experiment_id)
        .subquery()
    )

    # -----------------------------------------------------------------
    # Latest task author info (via task.experiment_id)
    # -----------------------------------------------------------------
    latest_task = (
        select(
            TaskModel.experiment_id.label("experiment_id"),
            TaskModel.user.label("last_user"),
            TaskModel.tags["github_username"].astext.label("last_github_username"),
            TaskModel.tags["github_meta"].astext.label("last_github_meta"),
        )
        .where(TaskModel.org_id == org_id, TaskModel.experiment_id.isnot(None))
        .order_by(
            TaskModel.experiment_id.asc(),
            TaskModel.created_at.desc(),
            TaskModel.id.desc(),
        )
        .distinct(TaskModel.experiment_id)
        .subquery()
    )

    # -----------------------------------------------------------------
    # Build experiment rows starting from ExperimentModel
    # -----------------------------------------------------------------
    experiment_rows = (
        select(
            ExperimentModel.id.label("experiment_id"),
            ExperimentModel.name.label("experiment_name"),
            case(
                (ExperimentModel.is_public.is_(True), 1), else_=0
            ).label("experiment_is_public"),
            func.coalesce(task_agg.c.task_count, 0).label("task_count"),
            func.coalesce(task_agg.c.analysis_tasks, 0).label("analysis_tasks"),
            func.coalesce(task_agg.c.verdict_good, 0).label("verdict_good"),
            func.coalesce(task_agg.c.verdict_needs_review, 0).label(
                "verdict_needs_review"
            ),
            func.coalesce(task_agg.c.verdict_failed, 0).label("verdict_failed"),
            func.coalesce(task_agg.c.verdict_pending, 0).label("verdict_pending"),
            func.greatest(
                task_agg.c.last_task_created_at,
                trial_timing.c.last_trial_created_at,
            ).label("last_created_at"),
            latest_task.c.last_user,
            latest_task.c.last_github_username,
            latest_task.c.last_github_meta,
        )
        .select_from(ExperimentModel)
        .outerjoin(task_agg, task_agg.c.experiment_id == ExperimentModel.id)
        .outerjoin(trial_timing, trial_timing.c.experiment_id == ExperimentModel.id)
        .outerjoin(latest_task, latest_task.c.experiment_id == ExperimentModel.id)
        .where(
            ExperimentModel.org_id == org_id,
            or_(
                task_agg.c.experiment_id.isnot(None),
                trial_timing.c.experiment_id.isnot(None),
            ),
        )
        .subquery()
    )

    # -----------------------------------------------------------------
    # Status filter helpers (use trial.experiment_id for correctness)
    # -----------------------------------------------------------------
    active_trial_exists = exists(
        select(1)
        .select_from(TrialModel)
        .where(
            TrialModel.org_id == org_id,
            TrialModel.experiment_id == experiment_rows.c.experiment_id,
            TrialModel.status.in_(
                [
                    TrialStatus.PENDING,
                    TrialStatus.QUEUED,
                    TrialStatus.RUNNING,
                    TrialStatus.RETRYING,
                ]
            ),
        )
    )
    failed_trial_exists = exists(
        select(1)
        .select_from(TrialModel)
        .where(
            TrialModel.org_id == org_id,
            TrialModel.experiment_id == experiment_rows.c.experiment_id,
            TrialModel.status == TrialStatus.FAILED,
        )
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
        await session.execute(
            query.order_by(
                nulls_last(experiment_rows.c.last_created_at.desc()),
                experiment_rows.c.experiment_id.asc(),
            )
            .limit(experiments_limit + 1)
            .offset(experiments_offset)
        )
    ).mappings().all()

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
                "active_trials": max(0, total_trials - completed_trials - failed_trials),
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
