"""Experiment-level aggregation helpers.

These are pure async functions that accept an ``AsyncSession`` and return
typed Pydantic models so they can be unit-tested without the web layer.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import (
    TaskModel,
    TaskVersionModel,
    TrialModel,
    task_experiments,
)
from oddish.schemas import ExperimentProbeRow, OrgProbeRow


async def list_experiment_probes_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None,
) -> list[ExperimentProbeRow]:
    """Return the most recent probe trial per task in the experiment.

    For each task in *experiment_id* that has at least one probe trial for its
    current version, exactly one row is returned — the row for the most recently
    created probe trial (newest ``TrialModel.created_at``).  Tasks with no
    probe trials are omitted.

    Columns returned per row: ``task_id``, ``task_name``, ``version``,
    ``model``, ``status``, ``probe_trial_id``.

    ``org_id`` is required when running in the cloud (multi-tenant).  Pass
    ``None`` only in single-tenant / OSS contexts where no org scoping exists.
    """
    stmt = (
        select(
            TaskModel,
            TaskVersionModel.version.label("version"),
            TrialModel,
        )
        .join(
            task_experiments,
            (task_experiments.c.task_id == TaskModel.id)
            & (task_experiments.c.experiment_id == experiment_id)
            & (task_experiments.c.deleted_at.is_(None)),
        )
        .join(
            TaskVersionModel,
            TaskVersionModel.id == TaskModel.current_version_id,
            isouter=True,
        )
        .join(
            TrialModel,
            (TrialModel.task_version_id == TaskModel.current_version_id)
            & TrialModel.is_probe.is_(True),
        )
        .order_by(TrialModel.created_at.desc())
    )
    if org_id is not None:
        stmt = stmt.where(TaskModel.org_id == org_id)

    result = await session.execute(stmt)

    rows: list[ExperimentProbeRow] = []
    seen: set[str] = set()
    for task, version, trial in result.all():
        if task.id in seen:
            # Keep only the most recent probe trial per task.
            continue
        seen.add(task.id)
        rows.append(
            ExperimentProbeRow(
                task_id=task.id,
                task_name=task.name,
                version=version,
                model=trial.model,
                status=getattr(trial.status, "value", trial.status),
                probe_trial_id=trial.id,
            )
        )
    return rows


async def list_org_probes_core(
    session: AsyncSession,
    *,
    org_id: str | None,
) -> list[OrgProbeRow]:
    """Return one row per task in the org that has at least one probe trial.

    Each row carries the task's total probe-run count plus the timestamp and
    status of its most recent probe trial. Rows are ordered most-recent-first
    by ``last_run_at``. Tasks with no probe trials are omitted.

    ``org_id`` is required in the cloud (multi-tenant). Pass ``None`` only in
    single-tenant / OSS contexts where no org scoping exists.
    """
    # Aggregate per task in SQL — one row out per task, not one per trial.
    # The old implementation selected full ``TrialModel`` rows (dragging the
    # unused ``result``/``analysis``/``harbor_config``/``phase_timing`` JSONB
    # blobs over the wire) for *every* probe trial and folded them in Python;
    # work scaled with total trial count. Here a single windowed pass over the
    # org's probe trials computes the per-task count and ranks each task's
    # trials newest-first, selecting only the five scalar columns the row
    # needs. The org filter sits on ``TrialModel.org_id`` (mirrors the task's
    # org at trial creation) so Postgres restricts the scan before windowing.
    ranked_select = select(
        TrialModel.task_id.label("task_id"),
        TrialModel.status.label("last_status"),
        TrialModel.created_at.label("last_run_at"),
        func.count().over(partition_by=TrialModel.task_id).label("run_count"),
        func.row_number()
        .over(
            partition_by=TrialModel.task_id,
            order_by=TrialModel.created_at.desc(),
        )
        .label("rn"),
    ).where(TrialModel.is_probe.is_(True))
    if org_id is not None:
        ranked_select = ranked_select.where(TrialModel.org_id == org_id)
    ranked = ranked_select.subquery()

    stmt = (
        select(
            TaskModel.name.label("task_name"),
            ranked.c.task_id,
            ranked.c.run_count,
            ranked.c.last_run_at,
            ranked.c.last_status,
        )
        .join(ranked, ranked.c.task_id == TaskModel.id)
        .where(ranked.c.rn == 1)
        .order_by(ranked.c.last_run_at.desc())
    )

    result = await session.execute(stmt)
    return [
        OrgProbeRow(
            task_id=row.task_id,
            task_name=row.task_name,
            run_count=row.run_count,
            last_run_at=row.last_run_at,
            last_status=getattr(row.last_status, "value", row.last_status),
        )
        for row in result.all()
    ]
