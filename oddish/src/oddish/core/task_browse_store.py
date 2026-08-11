from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.task_browse_contribution import (
    TrialBrowseContribution,
    build_trial_browse_contribution,
)
from oddish.core.task_browse_delta import Number
from oddish.db import (
    TaskBrowseTrialProjectionModel,
    TaskVersionBrowseAgentRollupModel,
    TaskVersionBrowseCostRollupModel,
    TaskVersionBrowseMetricValueModel,
    TaskVersionBrowseRollupModel,
    TrialModel,
    utcnow,
)

_TRIAL_COLUMNS = (
    TrialModel.id,
    TrialModel.name,
    TrialModel.task_id,
    TrialModel.task_version_id,
    TrialModel.org_id,
    TrialModel.idempotency_key,
    TrialModel.superseded_by_trial_id,
    TrialModel.deleted_at,
    TrialModel.is_probe,
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
    TrialModel.total_steps,
    TrialModel.billed_user_id,
    TrialModel.started_at,
    TrialModel.finished_at,
    TrialModel.created_at,
)


async def load_current_contributions(
    session: AsyncSession, trial_ids: list[str]
) -> dict[str, TrialBrowseContribution | None]:
    rows = (
        await session.execute(
            select(*_TRIAL_COLUMNS)
            .where(TrialModel.id.in_(trial_ids))
            .execution_options(include_deleted=True)
        )
    ).mappings()
    return {str(row["id"]): build_trial_browse_contribution(row) for row in rows}


async def load_stored_contributions(
    session: AsyncSession, trial_ids: list[str], *, lock: bool
) -> dict[str, TrialBrowseContribution]:
    statement = select(TaskBrowseTrialProjectionModel).where(
        TaskBrowseTrialProjectionModel.trial_id.in_(trial_ids)
    )
    if lock:
        statement = statement.with_for_update()
    rows = (await session.execute(statement)).scalars()
    fields = TrialBrowseContribution.__dataclass_fields__
    return {
        row.trial_id: TrialBrowseContribution(
            **{field: getattr(row, field) for field in fields}
        )
        for row in rows
    }


async def lock_version_rollups(session: AsyncSession, version_ids: set[str]) -> None:
    if not version_ids:
        return
    ids = sorted(version_ids)
    await session.execute(
        pg_insert(TaskVersionBrowseRollupModel)
        .values([{"task_version_id": version_id} for version_id in ids])
        .on_conflict_do_nothing(index_elements=["task_version_id"])
    )
    await session.execute(
        select(TaskVersionBrowseRollupModel.task_version_id)
        .where(TaskVersionBrowseRollupModel.task_version_id.in_(ids))
        .order_by(TaskVersionBrowseRollupModel.task_version_id)
        .with_for_update()
    )


async def store_contribution(
    session: AsyncSession,
    contribution: TrialBrowseContribution | None,
    *,
    trial_id: str,
    existed: bool,
) -> None:
    if contribution is None:
        if existed:
            await session.execute(
                delete(TaskBrowseTrialProjectionModel).where(
                    TaskBrowseTrialProjectionModel.trial_id == trial_id
                )
            )
        return
    values = contribution.values() | {"projected_at": utcnow()}
    await session.execute(
        pg_insert(TaskBrowseTrialProjectionModel)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["trial_id"],
            set_={key: value for key, value in values.items() if key != "trial_id"},
        )
    )


async def apply_version_deltas(
    session: AsyncSession, deltas: Mapping[str, Mapping[str, Number]]
) -> None:
    for version_id, delta in deltas.items():
        values: dict[str, Any] = {
            field: getattr(TaskVersionBrowseRollupModel, field) + value
            for field, value in delta.items()
            if value
        }
        if values:
            values["updated_at"] = utcnow()
            await session.execute(
                update(TaskVersionBrowseRollupModel)
                .where(TaskVersionBrowseRollupModel.task_version_id == version_id)
                .values(**values)
            )


async def apply_group_deltas(
    session: AsyncSession,
    deltas: Mapping[tuple[str, str, str], Mapping[str, Number]],
) -> None:
    for (version_id, agent, model_key), delta in deltas.items():
        if not any(delta.values()):
            continue
        await session.execute(
            pg_insert(TaskVersionBrowseAgentRollupModel)
            .values(
                task_version_id=version_id,
                agent=agent,
                model_key=model_key,
                **delta,
                updated_at=utcnow(),
            )
            .on_conflict_do_update(
                index_elements=["task_version_id", "agent", "model_key"],
                set_={
                    field: getattr(TaskVersionBrowseAgentRollupModel, field) + value
                    for field, value in delta.items()
                }
                | {"updated_at": utcnow()},
            )
        )
    if deltas:
        await session.execute(
            delete(TaskVersionBrowseAgentRollupModel).where(
                TaskVersionBrowseAgentRollupModel.task_version_id.in_(
                    sorted({key[0] for key in deltas})
                ),
                TaskVersionBrowseAgentRollupModel.trial_count <= 0,
            )
        )


async def apply_metric_value_deltas(
    session: AsyncSession,
    deltas: Mapping[tuple[str, str, str, str, float], int],
) -> None:
    for (version_id, agent, model_key, metric, value), delta in deltas.items():
        if not delta:
            continue
        await session.execute(
            pg_insert(TaskVersionBrowseMetricValueModel)
            .values(
                task_version_id=version_id,
                agent=agent,
                model_key=model_key,
                metric=metric,
                value=value,
                trial_count=delta,
            )
            .on_conflict_do_update(
                index_elements=[
                    "task_version_id",
                    "agent",
                    "model_key",
                    "metric",
                    "value",
                ],
                set_={
                    "trial_count": (
                        TaskVersionBrowseMetricValueModel.trial_count + delta
                    )
                },
            )
        )
    if deltas:
        await session.execute(
            delete(TaskVersionBrowseMetricValueModel).where(
                TaskVersionBrowseMetricValueModel.task_version_id.in_(
                    sorted({key[0] for key in deltas})
                ),
                TaskVersionBrowseMetricValueModel.trial_count <= 0,
            )
        )


async def apply_cost_deltas(
    session: AsyncSession,
    deltas: Mapping[tuple[str, str, datetime], Mapping[str, Number]],
) -> None:
    for (version_id, model_key, finished_at), delta in deltas.items():
        if not any(delta.values()):
            continue
        await session.execute(
            pg_insert(TaskVersionBrowseCostRollupModel)
            .values(
                task_version_id=version_id,
                model_key=model_key,
                finished_at=finished_at,
                **delta,
            )
            .on_conflict_do_update(
                index_elements=["task_version_id", "model_key", "finished_at"],
                set_={
                    field: getattr(TaskVersionBrowseCostRollupModel, field) + value
                    for field, value in delta.items()
                },
            )
        )
    if deltas:
        await session.execute(
            delete(TaskVersionBrowseCostRollupModel).where(
                TaskVersionBrowseCostRollupModel.task_version_id.in_(
                    sorted({key[0] for key in deltas})
                ),
                TaskVersionBrowseCostRollupModel.trial_count <= 0,
            )
        )


async def refresh_version_recency(
    session: AsyncSession, version_ids: Iterable[str]
) -> None:
    for version_id in sorted(version_ids):
        last_run_at = await session.scalar(
            select(func.max(TaskBrowseTrialProjectionModel.activity_at)).where(
                TaskBrowseTrialProjectionModel.task_version_id == version_id
            )
        )
        await session.execute(
            update(TaskVersionBrowseRollupModel)
            .where(TaskVersionBrowseRollupModel.task_version_id == version_id)
            .values(last_run_at=last_run_at, updated_at=utcnow())
        )
