"""Whole-experiment cost rollup.

Cost here means **spend**: every trial that actually ran under this experiment,
because every one of them burned tokens and was billed. That is deliberately a
wider set than the trials the grid renders, which are filtered to each task's
effective version and exclude superseded retries and probes -- filters that
exist so *scores* compare like with like, not because that money wasn't spent.
Billing already takes the same view: the quota sum (``core.quotas``) applies no
version, superseded, or probe filter, and the admin spend filter keeps billed
probes too. So this counts all versions, superseded retries, and probes, and the
tile's tooltip tells the reader the table below is a filtered view of it.

"Ran under this experiment" means owned by it: ``trials.experiment_id`` only.
Trials gathered into a collection (``experiment_trials``) or reached through a
shared task's link rows (``task_experiments``) render in the grid so scores can
be compared, but their spend belongs to -- and is already reported by -- their
home experiment. Counting them here would show the same dollars under two
experiments at once (and inflate rollup views assembled from other experiments'
work), so membership rows never contribute cost.

**Soft-deleted trials count too**, which is why the query opts out of the global
soft-delete filter with ``include_deleted=True`` (``db.soft_delete``). Deleting a
trial removes it from the *view*; it does not refund it. Both other places that
answer "what was spent" already do this -- the quota sum (``core.quotas``) and
the admin cost breakdown (``core.admin.get_cost_breakdown_core``) -- so counting
them here keeps one number across the page, the admin table, and the invoice.

The other reason the number can't be computed client-side: the page paginates
trials, so summing the loaded pages only ever covers a prefix of the experiment.

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
from oddish.db.models import TrialModel
from oddish.model_pricing import estimate_cost_usd
from oddish.schemas import ExperimentCostTotals


def _non_negative(column):
    """``max(0, column)`` -- spelled with CASE rather than GREATEST so it runs on
    SQLite as well as Postgres.

    Applied per row, not to the pooled total, because ``estimate_cost_usd``
    clamps each token bucket per row (``model_pricing``). Summing raw and
    clamping once at the end would let a negative count cancel a sibling row's
    positive one, which per-trial pricing never does.
    """
    return case((column > 0, column), else_=0)


_INPUT = _non_negative(func.coalesce(TrialModel.input_tokens, 0))
_OUTPUT = _non_negative(func.coalesce(TrialModel.output_tokens, 0))
_CACHED = _non_negative(func.coalesce(TrialModel.cache_tokens, 0))
_CACHE_WRITE = _non_negative(func.coalesce(TrialModel.cache_write_tokens, 0))

# max(0, input - cached - cache_write), per row.
_uncached_input = _non_negative(_INPUT - _CACHED - _CACHE_WRITE)

_HAS_NATIVE_COST = TrialModel.cost_usd.isnot(None)

# Matches the frontend usage fold: a trial contributes when either reported
# input or output usage, and the displayed total is input + output tokens.
_HAS_TOKEN_USAGE = or_(
    TrialModel.input_tokens.isnot(None), TrialModel.output_tokens.isnot(None)
)

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


def experiment_cost_groups_select(
    experiment_id: str, *, org_id: str | None = None
) -> Select:
    """One row per ``(agent, model, billed)`` bucket of the experiment's trials.

    Ownership is the only filter: a trial counts iff ``trials.experiment_id``
    is this experiment. Gathered / shared-task trials are excluded -- their
    spend is reported by their home experiment (module docstring). No version /
    superseded / probe / soft-delete filtering: those hide trials from the
    grid, but the spend is real and already billed.
    """
    billed = TrialModel.billed_user_id.isnot(None)

    query = (
        select(
            TrialModel.agent.label("agent"),
            TrialModel.model.label("model"),
            billed.label("billed"),
            func.count().label("trial_count"),
            func.count(case((_HAS_NATIVE_COST, 1))).label("native_count"),
            _sum_when(_HAS_NATIVE_COST, TrialModel.cost_usd).label("native_cost_usd"),
            func.count(case((_HAS_TOKEN_USAGE, 1))).label("token_trial_count"),
            _sum_when(_HAS_TOKEN_USAGE, _INPUT + _OUTPUT).label("token_count"),
            func.count(case((_ESTIMATED_ROW, 1))).label("estimated_count"),
            _sum_when(_ESTIMATED_ROW, _uncached_input).label("uncached_input_tokens"),
            _sum_when(_ESTIMATED_ROW, _CACHED).label("cached_tokens"),
            _sum_when(_ESTIMATED_ROW, _CACHE_WRITE).label("cache_write_tokens"),
            _sum_when(_ESTIMATED_ROW, _OUTPUT).label("output_tokens"),
        )
        .where(TrialModel.experiment_id == experiment_id)
        .group_by(TrialModel.agent, TrialModel.model, billed)
        .execution_options(include_deleted=True)
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
        # ``or model``: _resolve_trial_cost prices with ``model_name or
        # trial.model``, falling back to the raw string when normalization
        # returns None. Mirror it so the two never diverge.
        settings.normalize_trial_model(agent, model) or model,
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
        totals.token_count += row.token_count
        totals.token_trial_count += row.token_trial_count
        if row.billed:
            totals.billed_token_count += row.token_count
            totals.billed_token_trial_count += row.token_trial_count

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
