from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.task_browse_delta import (
    Number,
    add_delta,
    contribution_delta,
    group_delta,
    metric_values,
)
from oddish.core.task_browse_store import (
    apply_cost_deltas,
    apply_group_deltas,
    apply_metric_value_deltas,
    apply_version_deltas,
    load_current_contributions,
    load_stored_contributions,
    lock_version_rollups,
    refresh_version_recency,
    store_contribution,
)


async def project_task_browse_trials(
    session: AsyncSession, trial_ids: Iterable[str]
) -> None:
    """Apply idempotent old→new browser deltas for the named trials."""
    ids = sorted(set(trial_ids))
    if not ids:
        return
    current = await load_current_contributions(session, ids)
    initial_old = await load_stored_contributions(session, ids, lock=False)
    version_ids = {
        item.task_version_id for item in current.values() if item is not None
    } | {item.task_version_id for item in initial_old.values()}
    await lock_version_rollups(session, version_ids)
    old = await load_stored_contributions(session, ids, lock=True)

    version_deltas: dict[str, dict[str, Number]] = defaultdict(dict)
    group_deltas: dict[tuple[str, str, str], dict[str, Number]] = defaultdict(dict)
    metric_deltas: dict[tuple[str, str, str, str, float], int] = defaultdict(int)
    cost_deltas: dict[tuple[str, str, datetime], dict[str, Number]] = defaultdict(dict)
    for trial_id in ids:
        before = old.get(trial_id)
        after = current.get(trial_id)
        if before is not None:
            add_delta(
                version_deltas[before.task_version_id], contribution_delta(before, -1)
            )
            add_delta(
                group_deltas[(before.task_version_id, before.agent, before.model_key)],
                group_delta(before, -1),
            )
            for metric, value in metric_values(before).items():
                key = (
                    before.task_version_id,
                    before.agent,
                    before.model_key,
                    metric,
                    value,
                )
                metric_deltas[key] -= 1
            if (
                not before.is_mutable
                and before.finished_at is not None
                and before.resolved_cost_usd is not None
            ):
                add_delta(
                    cost_deltas[
                        (
                            before.task_version_id,
                            before.model_key,
                            before.finished_at,
                        )
                    ],
                    {"cost_usd": -before.resolved_cost_usd, "trial_count": -1},
                )
        if after is not None:
            add_delta(
                version_deltas[after.task_version_id], contribution_delta(after, 1)
            )
            add_delta(
                group_deltas[(after.task_version_id, after.agent, after.model_key)],
                group_delta(after, 1),
            )
            for metric, value in metric_values(after).items():
                key = (
                    after.task_version_id,
                    after.agent,
                    after.model_key,
                    metric,
                    value,
                )
                metric_deltas[key] += 1
            if (
                not after.is_mutable
                and after.finished_at is not None
                and after.resolved_cost_usd is not None
            ):
                add_delta(
                    cost_deltas[
                        (
                            after.task_version_id,
                            after.model_key,
                            after.finished_at,
                        )
                    ],
                    {"cost_usd": after.resolved_cost_usd, "trial_count": 1},
                )
        await store_contribution(
            session, after, trial_id=trial_id, existed=before is not None
        )

    await apply_version_deltas(session, version_deltas)
    await apply_group_deltas(session, group_deltas)
    await apply_metric_value_deltas(session, metric_deltas)
    await apply_cost_deltas(session, cost_deltas)
    await refresh_version_recency(session, version_ids)
