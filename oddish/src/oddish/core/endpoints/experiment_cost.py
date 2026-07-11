"""Whole-experiment cost rollup.

The experiment page paginates trials, so a client-side sum of ``cost_usd`` only
ever covers the pages that happen to be loaded. This computes the total over
every in-scope trial in one grouped query.

Cost is only *partly* stored: ``trials.cost_usd`` is NULL whenever the agent
runtime reported no native cost, and the API fills that in at read time from
token counts x the pricing table (``_resolve_trial_cost``). So a bare
``SUM(cost_usd)`` would silently drop every estimated trial.

Instead we group by ``(agent, model, billed)`` -- the key
``settings.normalize_trial_model`` prices on -- and, for the NULL-cost rows,
sum token counts rather than dollars. ``estimate_cost_usd`` is linear in tokens
for a fixed model, and its only per-row non-linearities are reproduced in SQL:

* the ``max(0, input - cached - cache_write)`` clamp (``_uncached_input``), and
* the "no usable tokens -> no cost" guard (``_ESTIMATED_ROW``).

Each group is therefore priced exactly once and the result equals the
per-trial sum. Feeding the pre-clamped totals back through
``estimate_cost_usd`` re-derives the same split (its clamp is idempotent on
already-clamped inputs), so pricing lives in exactly one place.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db.models import TaskModel, TaskVersionModel, TrialModel
from oddish.model_pricing import estimate_cost_usd
from oddish.schemas import ExperimentCostTotals

_INPUT = func.coalesce(TrialModel.input_tokens, 0)
_OUTPUT = func.coalesce(TrialModel.output_tokens, 0)
_CACHED = func.coalesce(TrialModel.cache_tokens, 0)
_CACHE_WRITE = func.coalesce(TrialModel.cache_write_tokens, 0)

# max(0, input - cached - cache_write), spelled with CASE rather than GREATEST
# so it runs on SQLite as well as Postgres.
_uncached_input = case(
    (_INPUT - _CACHED - _CACHE_WRITE > 0, _INPUT - _CACHED - _CACHE_WRITE),
    else_=0,
)

_HAS_NATIVE_COST = TrialModel.cost_usd.isnot(None)

# Mirrors ``_resolve_trial_cost`` + ``estimate_cost_usd``: the former bails when
# both token columns are NULL, the latter when no billable token bucket is
# positive. A trial failing either resolves to no cost at all.
_ESTIMATED_ROW = and_(
    TrialModel.cost_usd.is_(None),
    or_(TrialModel.input_tokens.isnot(None), TrialModel.output_tokens.isnot(None)),
    or_(_INPUT > 0, _OUTPUT > 0, _CACHE_WRITE > 0),
)


def _sum_when(condition, value):
    return func.coalesce(func.sum(case((condition, value), else_=0)), 0)


def _in_scope(experiment_id: str):
    """The trials the experiment grid considers at all, before version scoping."""
    return (
        trial_in_experiment(experiment_id),
        TrialModel.is_probe.is_(False),
        TrialModel.superseded_by_trial_id.is_(None),
    )


def _effective_version_select(experiment_id: str) -> Select:
    """Per task, the ``task_version_id`` the experiment page displays.

    SQL twin of ``resolve_effective_version_id``: the latest version among the
    task's in-scope trials. Ordered by the *integer* ``task_versions.version``,
    because ``task_version_id`` sorts lexicographically and would put ``-v9``
    above ``-v10``. Mirrors ``fetch_experiment_effective_version_ids``.
    """
    return (
        select(
            TrialModel.task_id.label("task_id"),
            TrialModel.task_version_id.label("task_version_id"),
        )
        .join(TaskVersionModel, TaskVersionModel.id == TrialModel.task_version_id)
        .where(*_in_scope(experiment_id), TrialModel.task_version_id.isnot(None))
        .order_by(TrialModel.task_id.asc(), TaskVersionModel.version.desc())
        .distinct(TrialModel.task_id)
    )


def experiment_cost_groups_select(
    experiment_id: str, *, org_id: str | None = None
) -> Select:
    """One row per ``(agent, model, billed)`` bucket of the experiment's trials.

    Scoped to exactly the trials the grid renders: non-probe, non-superseded,
    collection-aware, and — critically — restricted to each task's *effective
    version*. A task re-uploaded and re-run inside the same experiment keeps its
    older versions' trials in the DB, and the grid does not show them
    (``get_task_status_trials``), so counting them here would overstate cost.
    """
    billed = TrialModel.billed_user_id.isnot(None)
    effective = _effective_version_select(experiment_id).subquery()

    # ``COALESCE(latest in-scope version, task.current_version_id)`` — the
    # fallback ``resolve_effective_version_id`` uses when no in-scope trial
    # carries a version. A NULL effective means the task is unversioned, and
    # every live trial shows; otherwise only exact matches do (so a NULL
    # ``task_version_id`` against a versioned task drops out, as in Python).
    effective_version_id = func.coalesce(
        effective.c.task_version_id, TaskModel.current_version_id
    )

    query = (
        select(
            TrialModel.agent.label("agent"),
            TrialModel.model.label("model"),
            billed.label("billed"),
            func.count().label("trial_count"),
            func.count(case((_HAS_NATIVE_COST, 1))).label("native_count"),
            _sum_when(_HAS_NATIVE_COST, TrialModel.cost_usd).label("native_cost_usd"),
            func.count(case((_ESTIMATED_ROW, 1))).label("estimated_count"),
            _sum_when(_ESTIMATED_ROW, _uncached_input).label("uncached_input_tokens"),
            _sum_when(_ESTIMATED_ROW, _CACHED).label("cached_tokens"),
            _sum_when(_ESTIMATED_ROW, _CACHE_WRITE).label("cache_write_tokens"),
            _sum_when(_ESTIMATED_ROW, _OUTPUT).label("output_tokens"),
        )
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .join(effective, effective.c.task_id == TrialModel.task_id, isouter=True)
        .where(
            *_in_scope(experiment_id),
            or_(
                effective_version_id.is_(None),
                TrialModel.task_version_id == effective_version_id,
            ),
        )
        .group_by(TrialModel.agent, TrialModel.model, billed)
    )
    if org_id is not None:
        query = query.where(TrialModel.org_id == org_id)
    return query


def _estimated_group_cost(agent: str | None, model: str | None, row) -> float | None:
    """Price one group's pooled token totals, or ``None`` if the model has no
    pricing (matching ``_resolve_trial_cost``, which yields no cost there)."""
    if not row.estimated_count:
        return None
    return estimate_cost_usd(
        settings.normalize_trial_model(agent, model),
        # Re-derives ``uncached_input_tokens`` under estimate_cost_usd's own
        # clamp, which is a no-op on an already-clamped total.
        row.uncached_input_tokens + row.cached_tokens + row.cache_write_tokens,
        row.output_tokens,
        row.cached_tokens,
        row.cache_write_tokens,
    )


def fold_experiment_cost_groups(rows) -> ExperimentCostTotals:
    """Fold ``experiment_cost_groups_select`` rows into the UI's cost rollup."""
    totals = ExperimentCostTotals()

    for row in rows:
        totals.total_trials += row.trial_count

        priced: list[tuple[float, int, bool]] = []
        if row.native_count:
            priced.append((float(row.native_cost_usd), row.native_count, False))
        estimated = _estimated_group_cost(row.agent, row.model, row)
        if estimated is not None:
            priced.append((estimated, row.estimated_count, True))

        for cost_usd, trial_count, is_estimated in priced:
            totals.cost_usd += cost_usd
            totals.cost_trial_count += trial_count
            if is_estimated:
                totals.cost_has_estimated = True
            else:
                totals.cost_has_native = True
            if not row.billed:
                continue
            totals.billed_cost_usd += cost_usd
            totals.billed_trial_count += trial_count
            if is_estimated:
                totals.billed_has_estimated = True
            else:
                totals.billed_has_native = True

    return totals


async def get_experiment_cost_totals(
    session: AsyncSession, *, experiment_id: str, org_id: str | None = None
) -> ExperimentCostTotals:
    """Cost rollup over every trial in the experiment, independent of paging."""
    result = await session.execute(
        experiment_cost_groups_select(experiment_id, org_id=org_id)
    )
    return fold_experiment_cost_groups(result.all())
