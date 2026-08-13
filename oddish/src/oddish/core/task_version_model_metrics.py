from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Integer, case, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.task_browse_metrics import browse_trial_scope, trial_bucket_label
from oddish.db import (
    TaskVersionModel,
    TaskVersionModelMetricsModel,
    TrialModel,
    TrialStatus,
)

_INFLIGHT = (
    TrialStatus.PENDING,
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
)

_ENV_STAGES = (
    "starting",
    "trial_started",
    "environment_setup",
    "image_build_failed",
)

_CANCELLED_USER = "Cancelled by user"
_CANCELLED_REAPED = "Worker heartbeat stalled%"

# Written by the recompute; everything else in the row is part of the key.
_VALUE_FIELDS = (
    "task_id",
    "n_pass",
    "n_partial",
    "n_fail",
    "n_unscored_agent",
    "n_unscored_env",
    "n_unscored_verify",
    "n_cancelled_user",
    "n_cancelled_reaped",
    "n_cancelled_other",
    "n_skipped",
    "n_scoreless",
    "n_unscored_unknown",
    "n_inflight",
    "sum_reward",
    "n_reward_present",
    "sum_runtime",
    "n_runtime_present",
    "n_with_trajectory",
    "n_steps_present",
    "sum_steps",
    "steps_all_min",
    "steps_all_p50",
    "steps_all_max",
    "steps_pass_n",
    "steps_pass_min",
    "steps_pass_p50",
    "steps_pass_max",
    "steps_fail_n",
    "steps_fail_min",
    "steps_fail_p50",
    "steps_fail_max",
    "steps_partial_n",
)


def _count_where(condition: Any) -> Any:
    return func.count(case((condition, 1)))


def _median(condition: Any = None) -> Any:
    """Observed median step count.

    ``percentile_disc`` rather than ``_cont``: most groups hold four trials or
    fewer, where interpolation returns a step count no trial ever had.
    """
    expr = func.percentile_disc(0.5).within_group(TrialModel.total_steps.asc())
    return expr.filter(condition) if condition is not None else expr


def _step_block(prefix: str, condition: Any) -> list[Any]:
    steps = TrialModel.total_steps
    return [
        # count() of a column skips NULLs, which is exactly the population the
        # min/median/max below are computed over.
        func.count(steps).filter(condition).label(f"{prefix}_n"),
        func.min(steps).filter(condition).label(f"{prefix}_min"),
        func.cast(_median(condition), Integer).label(f"{prefix}_p50"),
        func.max(steps).filter(condition).label(f"{prefix}_max"),
    ]


def metric_columns() -> list[Any]:
    """Aggregate expressions producing one metrics row per group."""
    bucket = trial_bucket_label()
    stage = TrialModel.harbor_stage
    error = TrialModel.error_message
    reward = TrialModel.reward
    steps = TrialModel.total_steps

    # Trials carry no duration column; wall-clock comes from the timestamps.
    runtime = func.extract(
        "epoch", TrialModel.finished_at - TrialModel.started_at
    )

    cancelled = stage == "cancelled"
    scored = reward.isnot(None)
    is_pass = bucket == "pass"
    is_fail = bucket == "fail"

    terminal_unscored = case(
        (stage == "agent_running", "agent"),
        (stage.in_(_ENV_STAGES), "env"),
        (stage == "verification", "verify"),
        (cancelled, "cancelled"),
        else_="unknown",
    )

    return [
        func.count(case((is_pass, 1))).label("n_pass"),
        func.count(case((bucket == "partial", 1))).label("n_partial"),
        func.count(case((is_fail, 1))).label("n_fail"),
        # Unscored attribution only applies to trials that never got a reward
        # and are not in flight; the bucket taxonomy owns everything else.
        _count_where(
            (~scored) & (terminal_unscored == "agent") & (bucket != "skipped")
        ).label("n_unscored_agent"),
        _count_where(
            (~scored) & (terminal_unscored == "env") & (bucket != "skipped")
        ).label("n_unscored_env"),
        _count_where(
            (~scored) & (terminal_unscored == "verify") & (bucket != "skipped")
        ).label("n_unscored_verify"),
        _count_where(cancelled & (error == _CANCELLED_USER)).label("n_cancelled_user"),
        _count_where(cancelled & error.like(_CANCELLED_REAPED)).label(
            "n_cancelled_reaped"
        ),
        _count_where(
            cancelled
            & ~func.coalesce(error, "").like(_CANCELLED_REAPED)
            & (func.coalesce(error, "") != _CANCELLED_USER)
        ).label("n_cancelled_other"),
        _count_where(bucket == "skipped").label("n_skipped"),
        _count_where(bucket == "scoreless").label("n_scoreless"),
        _count_where(
            (~scored)
            & (terminal_unscored == "unknown")
            & (~TrialModel.status.in_(_INFLIGHT))
            & (bucket.notin_(("skipped", "scoreless")))
        ).label("n_unscored_unknown"),
        _count_where(TrialModel.status.in_(_INFLIGHT)).label("n_inflight"),
        func.coalesce(func.sum(reward), 0.0).label("sum_reward"),
        func.count(reward).label("n_reward_present"),
        # Cancelled and reaped trials died early by definition; folding their
        # runtimes in drags the average toward zero.
        func.coalesce(func.sum(runtime).filter(~cancelled), 0.0).label("sum_runtime"),
        func.count(runtime).filter(~cancelled).label("n_runtime_present"),
        _count_where(TrialModel.has_trajectory.is_(True)).label("n_with_trajectory"),
        func.count(steps).label("n_steps_present"),
        func.coalesce(func.sum(steps), 0).label("sum_steps"),
        func.min(steps).label("steps_all_min"),
        func.cast(_median(), Integer).label("steps_all_p50"),
        func.max(steps).label("steps_all_max"),
        *_step_block("steps_pass", is_pass),
        *_step_block("steps_fail", is_fail),
        func.count(steps).filter(bucket == "partial").label("steps_partial_n"),
    ]


def metrics_query(version_ids: list[str]) -> Any:
    """Grouped metrics over the browse trial population for these versions."""
    return (
        select(
            TrialModel.task_version_id.label("task_version_id"),
            TrialModel.agent.label("agent"),
            func.coalesce(TrialModel.model, "").label("model"),
            func.min(TrialModel.task_id).label("task_id"),
            *metric_columns(),
        )
        .where(TrialModel.task_version_id.in_(version_ids), *browse_trial_scope())
        # Group on the raw column, not the coalesce: the SELECT and GROUP BY
        # coalesces compile to separate bind parameters, which Postgres will not
        # recognise as the same expression. Grouping on `model` is equivalent --
        # SQL groups NULLs together -- and the coalesce above only renames that
        # group to "" so it fits a non-nullable primary key.
        .group_by(TrialModel.task_version_id, TrialModel.agent, TrialModel.model)
    )


async def refresh_task_version_model_metrics(
    session: AsyncSession, task_version_ids: Iterable[str | None]
) -> None:
    """Rebuild affected (version, agent, model) rows inside the caller transaction.

    Recompute, never increment: a retried trial is reset to RUNNING with its
    reward and ``total_steps`` nulled (``trial_handler.py``), so trials move
    backwards out of terminal buckets and delta arithmetic would drift.
    """
    version_ids = sorted({str(value) for value in task_version_ids if value})
    if not version_ids:
        return
    await session.flush()

    # Lock here rather than relying on the caller. refresh_task_browse_summaries
    # already holds these, but the backfill calls this function directly, and an
    # unlocked backfill batch racing a live refresh can overwrite a fresh row
    # with the snapshot it aggregated moments earlier -- silent staleness, not a
    # crash. pg_advisory_xact_lock is re-entrant within a transaction, so taking
    # an already-held lock is free; the same sorted order keeps both paths in a
    # single global ordering and cannot deadlock against each other.
    for version_id in version_ids:
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(CAST(:version_id AS text), 0))"
            ),
            {"version_id": version_id},
        )

    known = (
        await session.execute(
            select(TaskVersionModel.id)
            .where(TaskVersionModel.id.in_(version_ids))
            .order_by(TaskVersionModel.id)
        )
    ).scalars().all()
    if not known:
        return

    rows = (await session.execute(metrics_query([str(v) for v in known]))).mappings()
    values = [dict(row) for row in rows]

    # A version whose last trial was deleted or superseded keeps no row, and a
    # group that lost its final trial must not survive as a stale one.
    present = {(v["task_version_id"], v["agent"], v["model"]) for v in values}
    existing = (
        await session.execute(
            select(
                TaskVersionModelMetricsModel.task_version_id,
                TaskVersionModelMetricsModel.agent,
                TaskVersionModelMetricsModel.model,
            ).where(TaskVersionModelMetricsModel.task_version_id.in_(known))
        )
    ).all()
    stale = [key for key in existing if tuple(key) not in present]
    for version_id, agent, model in stale:
        await session.execute(
            delete(TaskVersionModelMetricsModel).where(
                TaskVersionModelMetricsModel.task_version_id == version_id,
                TaskVersionModelMetricsModel.agent == agent,
                TaskVersionModelMetricsModel.model == model,
            )
        )

    if not values:
        return
    insert = pg_insert(TaskVersionModelMetricsModel).values(values)
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[
                TaskVersionModelMetricsModel.task_version_id,
                TaskVersionModelMetricsModel.agent,
                TaskVersionModelMetricsModel.model,
            ],
            set_={field: getattr(insert.excluded, field) for field in _VALUE_FIELDS}
            | {"updated_at": func.now()},
        )
    )
