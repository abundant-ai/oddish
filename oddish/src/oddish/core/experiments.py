"""Experiment-level aggregation helpers.

These are pure async functions that accept an ``AsyncSession`` and return
typed Pydantic models so they can be unit-tested without the web layer.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import (
    TaskModel,
    TaskVersionModel,
    TrialModel,
    task_experiments,
)
from oddish.schemas import ExperimentProbeRow


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
