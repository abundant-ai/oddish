from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.cost_basis import not_combine_copy_filter
from oddish.core.task_browse_projection import build_task_version_browse_projection
from oddish.db import TaskVersionModel, TrialModel

_ROLLUP_TRIAL_COLUMNS = (
    TrialModel.id,
    TrialModel.name,
    TrialModel.task_version_id,
    TrialModel.status,
    TrialModel.reward,
    TrialModel.error_message,
    TrialModel.agent,
    TrialModel.model,
    TrialModel.cost_usd,
    TrialModel.input_tokens,
    TrialModel.output_tokens,
    TrialModel.cache_tokens,
    TrialModel.cache_write_tokens,
    TrialModel.billed_user_id,
    TrialModel.started_at,
    TrialModel.finished_at,
    TrialModel.created_at,
)


async def refresh_task_version_browse_rollups(
    session: AsyncSession, version_ids: Iterable[str | None]
) -> None:
    """Rebuild task-browser projections for the named immutable versions."""
    ids = sorted({str(version_id) for version_id in version_ids if version_id})
    if not ids:
        return
    await session.execute(
        select(TaskVersionModel.id)
        .where(TaskVersionModel.id.in_(ids))
        .order_by(TaskVersionModel.id)
        .with_for_update()
    )
    rows = (
        await session.execute(
            select(*_ROLLUP_TRIAL_COLUMNS)
            .where(
                TrialModel.task_version_id.in_(ids),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.is_probe.isnot(True),
                not_combine_copy_filter(),
            )
            .order_by(
                TrialModel.task_version_id,
                TrialModel.created_at,
                TrialModel.id,
            )
        )
    ).mappings()
    by_version: dict[str, list] = defaultdict(list)
    for row in rows:
        by_version[str(row["task_version_id"])].append(row)
    for version_id in ids:
        last_run_at, summary = build_task_version_browse_projection(
            by_version.get(version_id, ())
        )
        await session.execute(
            update(TaskVersionModel)
            .where(TaskVersionModel.id == version_id)
            .values(browse_last_run_at=last_run_at, browse_rollup=summary)
        )


async def refresh_task_browse_rollups_for_trials(
    session: AsyncSession, trial_ids: Iterable[str]
) -> None:
    ids = sorted(set(trial_ids))
    if not ids:
        return
    version_ids = (
        await session.execute(
            select(TrialModel.task_version_id)
            .where(TrialModel.id.in_(ids))
            .execution_options(include_deleted=True)
        )
    ).scalars()
    await refresh_task_version_browse_rollups(session, version_ids)


async def refresh_task_browse_rollups_for_tasks(
    session: AsyncSession, task_ids: Iterable[str]
) -> None:
    ids = sorted(set(task_ids))
    if not ids:
        return
    version_ids = (
        await session.execute(
            select(TaskVersionModel.id).where(TaskVersionModel.task_id.in_(ids))
        )
    ).scalars()
    await refresh_task_version_browse_rollups(session, version_ids)
