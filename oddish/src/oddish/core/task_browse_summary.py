from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.task_browse_metrics import (
    browse_cost_group_columns,
    browse_trial_scope,
    trial_bucket_label,
)
from oddish.db import (
    TaskBrowseSummaryModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)

_SUMMARY_FIELDS = (
    "task_id",
    "last_run_at",
    "total_trials",
    "completed_trials",
    "failed_trials",
    "reward_success",
    "reward_sum",
    "reward_total",
    "pass_count",
    "partial_count",
    "fail_count",
    "harness_count",
    "skipped_count",
    "pending_count",
    "cost_breakdown",
)


def _empty_summary(task_id: str, version_id: str) -> dict:
    return {
        "task_version_id": version_id,
        "task_id": task_id,
        "last_run_at": None,
        "total_trials": 0,
        "completed_trials": 0,
        "failed_trials": 0,
        "reward_success": 0,
        "reward_sum": 0.0,
        "reward_total": 0,
        "pass_count": 0,
        "partial_count": 0,
        "fail_count": 0,
        "harness_count": 0,
        "skipped_count": 0,
        "pending_count": 0,
        "cost_breakdown": [],
    }


async def refresh_task_browse_summaries(
    session: AsyncSession, task_version_ids: Iterable[str | None]
) -> None:
    """Rebuild affected version summaries inside the caller transaction."""
    version_ids = sorted({str(value) for value in task_version_ids if value})
    if not version_ids:
        return
    await session.flush()
    # Serialize rebuilds without upgrading a task-version FK row lock. Trial
    # inserts already hold KEY SHARE on that row, so FOR UPDATE here can
    # deadlock when concurrent insert transactions both refresh one version.
    # Sorted transaction-scoped advisory locks preserve deterministic batch
    # ordering, and READ COMMITTED gives each aggregate statement a snapshot
    # taken after any preceding refresher commits.
    for version_id in version_ids:
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(CAST(:version_id AS text), 0))"
            ),
            {"version_id": version_id},
        )
    versions = (
        await session.execute(
            select(TaskVersionModel.id, TaskVersionModel.task_id)
            .where(TaskVersionModel.id.in_(version_ids))
            .order_by(TaskVersionModel.id)
        )
    ).all()
    summaries = {
        str(version_id): _empty_summary(str(task_id), str(version_id))
        for version_id, task_id in versions
    }
    if not summaries:
        return

    bucket = trial_bucket_label()
    activity = func.greatest(
        func.coalesce(TrialModel.finished_at, TrialModel.created_at),
        func.coalesce(TrialModel.started_at, TrialModel.created_at),
        TrialModel.created_at,
    )
    counts = await session.execute(
        select(
            TrialModel.task_version_id,
            func.max(activity).label("last_run_at"),
            func.count(TrialModel.id).label("total_trials"),
            func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
                "completed_trials"
            ),
            func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
                "failed_trials"
            ),
            func.count(case((TrialModel.reward == 1, 1))).label("reward_success"),
            func.coalesce(func.sum(TrialModel.reward), 0.0).label("reward_sum"),
            func.count(case((TrialModel.reward.isnot(None), 1))).label("reward_total"),
            *[
                func.count(case((bucket == name, 1))).label(
                    "pending_count" if name == "other" else f"{name}_count"
                )
                for name in ("pass", "partial", "fail", "harness", "skipped", "other")
            ],
        )
        .where(TrialModel.task_version_id.in_(list(summaries)), *browse_trial_scope())
        .group_by(TrialModel.task_version_id)
    )
    for row in counts.mappings():
        summary = summaries[str(row["task_version_id"])]
        for field in _SUMMARY_FIELDS[1:-1]:
            summary[field] = row[field]

    billed = TrialModel.billed_user_id.isnot(None)
    costs = await session.execute(
        select(
            TrialModel.task_version_id,
            TrialModel.agent,
            TrialModel.model,
            billed.label("billed"),
            *browse_cost_group_columns(),
        )
        .where(TrialModel.task_version_id.in_(list(summaries)), *browse_trial_scope())
        .group_by(
            TrialModel.task_version_id, TrialModel.agent, TrialModel.model, billed
        )
    )
    for row in costs.mappings():
        summaries[str(row["task_version_id"])]["cost_breakdown"].append(
            {
                "agent": str(row["agent"]),
                "model": row["model"],
                "billed": bool(row["billed"]),
                **{
                    field: float(row[field] or 0)
                    if field == "native_cost"
                    else int(row[field] or 0)
                    for field in (
                        "native_cost",
                        "native_trials",
                        "est_input",
                        "est_output",
                        "est_cache",
                        "est_cache_write",
                        "est_trials",
                    )
                },
            }
        )

    insert = pg_insert(TaskBrowseSummaryModel).values(list(summaries.values()))
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[TaskBrowseSummaryModel.task_version_id],
            set_={field: getattr(insert.excluded, field) for field in _SUMMARY_FIELDS}
            | {"updated_at": func.now()},
        )
    )
