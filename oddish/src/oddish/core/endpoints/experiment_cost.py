"""Whole-experiment cost rollup.

Two numbers per experiment, answering two different questions:

* ``cost_*`` -- what did the work on this page cost? Every trial the page can
  render counts: trials homed in the experiment (``trials.experiment_id``) and
  trials gathered into it (``experiment_trials``) -- the same membership the
  grid applies (``trial_in_experiment``). A collection assembled entirely from
  other experiments' work therefore shows the real price of that work, not a
  blank tile. ``task_experiments`` link rows deliberately do NOT widen this:
  they exist so shared/collected TASK rows appear, but the grid still scopes
  those tasks' trials to home-or-gathered, and counting a linked task's whole
  history would price unselected trials -- and let a frozen collection's cost
  drift upward whenever anyone reruns the same task elsewhere.
* ``owned_*`` -- what did this experiment itself spend (the page's "New
  spend")? Only trials homed in the experiment. This is the number that stays
  additive across experiments: gathered/linked trials are already reported as
  owned spend by their home experiment, so summing owned spend across pages
  never double-counts a dollar, while summing ``cost_*`` across pages does
  (deliberately -- both tiles say so).

``billed_*`` is the subset of **owned** spend attributed to a registered
user's quota (``billed_user_id``); owned-but-unbilled spend (imports,
unattributed submitters, offboarded users) keeps the two apart.

Spend is deliberately wider than the trials the grid renders, which are
filtered to each task's effective version and exclude superseded retries and
probes -- filters that exist so *scores* compare like with like, not because
that money wasn't spent. Billing already takes the same view: the quota sum
(``core.quotas``) applies no version, superseded, or probe filter, and the
admin spend filter keeps billed probes too. So this counts all versions,
superseded retries, and probes, and the tile's tooltip tells the reader the
table below is a filtered view of it.

Membership rows are the one place soft-deletes DO apply: a soft-deleted
``experiment_trials`` row means "removed from this collection", so its trial
stops counting here (it never stops counting on its home experiment).

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

Instead we group by ``(agent, model, billed, owned)`` -- the key
``settings.normalize_trial_model`` prices on, split by ownership -- and, for
the NULL-cost rows, sum token counts rather than dollars. ``estimate_cost_usd`` is linear in tokens
for a fixed model, and its only per-row non-linearities are reproduced in SQL:

* the ``max(0, input - cached - cache_write)`` clamp (``_uncached_input``), and
* the "no usable tokens -> no cost" guard (``_ESTIMATED_ROW``).

Each group is therefore priced exactly once and the result equals the
per-trial sum. Feeding the pre-clamped totals back through
``estimate_cost_usd`` re-derives the same split (its clamp is idempotent on
already-clamped inputs), so pricing lives in exactly one place.

On top of that inference number, ``get_experiment_cost_totals`` adds the trial's
two other spend components -- QA (the ``analysis_costs`` ledger) and compute (the
``modal_costs`` ledger) -- so the tile can show a composite ``total_*`` =
inference + qa + compute. Those are two aggregate sums (``_experiment_ledger
_totals_select``) over the *same* member population, under the same
membership/``include_deleted``/``org_id`` predicate and split into the same
``owned``/``billed`` scopes, so every composite total agrees with its parts.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db.models import AnalysisCostModel, ModalCostSpanModel, TrialModel
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
    """One row per ``(agent, model, billed, owned)`` bucket of the member trials.

    Membership is the only filter: a trial counts iff it is homed in this
    experiment or gathered into it -- ``trial_in_experiment``, the same
    predicate the grid scopes trials with, so the tile prices exactly the work
    the page can render (module docstring explains why ``task_experiments``
    links don't widen it). The single WHERE counts a trial once even when it
    is both homed and gathered. ``owned`` splits the groups so the fold can
    report home spend separately. No version / superseded / probe /
    trial-soft-delete filtering: those hide trials from the grid, but the
    spend is real and already billed.
    """
    billed = TrialModel.billed_user_id.isnot(None)
    owned = TrialModel.experiment_id == experiment_id
    member = trial_in_experiment(experiment_id)

    query = (
        select(
            TrialModel.agent.label("agent"),
            TrialModel.model.label("model"),
            billed.label("billed"),
            owned.label("owned"),
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
        .where(member)
        .group_by(TrialModel.agent, TrialModel.model, billed, owned)
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
        if row.owned:
            totals.owned_token_count += row.token_count
            totals.owned_token_trial_count += row.token_trial_count
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
            if not row.owned:
                continue
            totals.owned_cost_usd += cost_usd
            totals.owned_trial_count += trial_count
            if is_estimated:
                totals.owned_has_estimated = True
            else:
                totals.owned_has_native = True
            if not row.billed:
                continue
            totals.billed_cost_usd += cost_usd
            totals.billed_trial_count += trial_count
            if is_estimated:
                totals.billed_has_estimated = True
            else:
                totals.billed_has_native = True

    return totals


def _experiment_ledger_totals_select(
    ledger_model: type,
    cost_column,
    experiment_id: str,
    *,
    org_id: str | None = None,
    extra_filter=None,
) -> Select:
    """Total / owned / billed sums of a per-trial cost ledger over the member trials.

    The composite twin of :func:`experiment_cost_groups_select` for the QA
    (``analysis_costs``) and compute (``modal_costs``) ledgers. It prices the
    *same* member trials the inference fold does -- same ``trial_in_experiment``
    membership, same ``include_deleted``, same optional ``org_id`` -- split into
    the same three scopes (``owned`` = homed here, ``billed`` = the billed subset
    of owned). Adding qa/compute for exactly the trials whose inference is
    counted is what keeps the composite total consistent with its parts.

    Inner-joining the ledger to its trial counts each ledger row once even when
    the trial is both homed and gathered (the join keys on ``trial_id``), so this
    never needs a ``GROUP BY trial_id``. ``include_deleted`` keeps a soft-deleted
    member trial's ledger rows -- its inference is counted under the same option,
    so its qa/compute must be too. The ledger's own ``deleted_at`` is filtered
    explicitly: these models are not soft-delete-registered, and under
    ``include_deleted`` nothing would filter them otherwise -- mirroring
    :func:`oddish.core.cost_basis.composite_cost_by_trial`.
    """
    billed = TrialModel.billed_user_id.isnot(None)
    owned = TrialModel.experiment_id == experiment_id
    member = trial_in_experiment(experiment_id)

    query = (
        select(
            func.coalesce(func.sum(cost_column), 0.0).label("total"),
            func.coalesce(func.sum(case((owned, cost_column), else_=0.0)), 0.0).label(
                "owned"
            ),
            func.coalesce(
                func.sum(case((and_(owned, billed), cost_column), else_=0.0)), 0.0
            ).label("billed"),
        )
        .select_from(ledger_model)
        .join(TrialModel, ledger_model.trial_id == TrialModel.id)
        .where(member, ledger_model.deleted_at.is_(None))
        .execution_options(include_deleted=True)
    )
    if extra_filter is not None:
        query = query.where(extra_filter)
    if org_id is not None:
        query = query.where(TrialModel.org_id == org_id)
    return query


def _experiment_composite_only_count_select(
    experiment_id: str, *, org_id: str | None = None
) -> Select:
    """Member / owned / billed counts of trials priced ONLY by QA/compute.

    The grouped inference fold already counts every trial with priced inference
    (native cost, or a token row on a model the pricing table resolves) into
    ``*_trial_count``. This adds the trials that fold missed but whose QA/compute
    the ledger sums *did* include: a trial with **no priced inference at all**
    (``cost_usd IS NULL`` and no estimable token row -- ``~_ESTIMATED_ROW``) that
    carries non-zero QA or compute. Summed onto the fold's counts, the result is
    "trials contributing any composite component", so ``cost_trial_count`` never
    contradicts the composite ``total_*``.

    Excluding ``_ESTIMATED_ROW`` here (rather than a positive priced-ness test) is
    deliberate: whether a token row actually prices depends on the pricing table,
    which SQL can't consult, so an estimable-row trial is left to the fold to count
    (or not, when its model is unpriceable) and is never double-counted here.

    Counts each member trial once: ``trial_in_experiment`` is a predicate and the
    per-trial QA/compute presence is read via correlated scalar sub-selects (same
    ``deleted_at`` / NULL-cost filters as :func:`composite_cost_by_trial`), so no
    join can fan a trial out. ``include_deleted`` matches the inference fold.
    """
    billed = TrialModel.billed_user_id.isnot(None)
    owned = TrialModel.experiment_id == experiment_id
    member = trial_in_experiment(experiment_id)

    qa_sum = (
        select(func.coalesce(func.sum(AnalysisCostModel.cost_usd), 0.0))
        .where(
            AnalysisCostModel.trial_id == TrialModel.id,
            AnalysisCostModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .scalar_subquery()
    )
    compute_sum = (
        select(func.coalesce(func.sum(ModalCostSpanModel.cost_usd), 0.0))
        .where(
            ModalCostSpanModel.trial_id == TrialModel.id,
            ModalCostSpanModel.cost_usd.isnot(None),
            ModalCostSpanModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .scalar_subquery()
    )
    # No priced inference of either kind, but real QA or compute spend.
    composite_only = and_(
        TrialModel.cost_usd.is_(None),
        ~_ESTIMATED_ROW,
        or_(qa_sum > 0, compute_sum > 0),
    )

    query = (
        select(
            func.count(case((composite_only, 1))).label("total"),
            func.count(case((and_(owned, composite_only), 1))).label("owned"),
            func.count(case((and_(owned, billed, composite_only), 1))).label("billed"),
        )
        .select_from(TrialModel)
        .where(member)
        .execution_options(include_deleted=True)
    )
    if org_id is not None:
        query = query.where(TrialModel.org_id == org_id)
    return query


async def _add_experiment_composite_costs(
    session: AsyncSession,
    totals: ExperimentCostTotals,
    *,
    experiment_id: str,
    org_id: str | None,
) -> None:
    """Fold QA + compute ledger spend into ``totals`` over the inference population.

    Two aggregate queries (one per ledger), each returning the member / owned /
    billed sums the inference fold already produced for inference. ``total_*`` is
    inference + qa + compute at each scope -- the inference components were folded
    in by :func:`fold_experiment_cost_groups` before this runs. A third query adds
    the QA/compute-only trials (no priced inference) onto the fold's
    ``*_trial_count`` columns, so counts cover "any composite component" and stay
    consistent with the composite totals.
    """
    qa = (
        await session.execute(
            _experiment_ledger_totals_select(
                AnalysisCostModel,
                AnalysisCostModel.cost_usd,
                experiment_id,
                org_id=org_id,
            )
        )
    ).one()
    compute = (
        await session.execute(
            _experiment_ledger_totals_select(
                ModalCostSpanModel,
                ModalCostSpanModel.cost_usd,
                experiment_id,
                org_id=org_id,
                # Open/unpriced spans (NULL cost) are excluded, matching
                # composite_cost_by_trial and the admin cost dashboard.
                extra_filter=ModalCostSpanModel.cost_usd.isnot(None),
            )
        )
    ).one()

    composite_only = (
        await session.execute(
            _experiment_composite_only_count_select(experiment_id, org_id=org_id)
        )
    ).one()

    totals.qa_cost_usd = float(qa.total or 0.0)
    totals.owned_qa_cost_usd = float(qa.owned or 0.0)
    totals.billed_qa_cost_usd = float(qa.billed or 0.0)
    totals.compute_cost_usd = float(compute.total or 0.0)
    totals.owned_compute_cost_usd = float(compute.owned or 0.0)
    totals.billed_compute_cost_usd = float(compute.billed or 0.0)
    # Fold the QA/compute-only trials (no priced inference) into the trial counts
    # the grouped inference fold already produced, so the counts cover "any
    # composite component" and never contradict the composite ``total_*``.
    totals.cost_trial_count += int(composite_only.total or 0)
    totals.owned_trial_count += int(composite_only.owned or 0)
    totals.billed_trial_count += int(composite_only.billed or 0)
    totals.total_cost_usd = (
        totals.cost_usd + totals.qa_cost_usd + totals.compute_cost_usd
    )
    totals.owned_total_cost_usd = (
        totals.owned_cost_usd + totals.owned_qa_cost_usd + totals.owned_compute_cost_usd
    )
    totals.billed_total_cost_usd = (
        totals.billed_cost_usd
        + totals.billed_qa_cost_usd
        + totals.billed_compute_cost_usd
    )


async def get_experiment_cost_totals(
    session: AsyncSession, *, experiment_id: str, org_id: str | None = None
) -> ExperimentCostTotals:
    """Cost rollup over every member trial, independent of paging.

    Inference is priced by the grouped fold; QA + compute are then summed over
    the identical member population (and owned/billed subsets) so the composite
    ``total_*`` fields agree with their inference parts.
    """
    result = await session.execute(
        experiment_cost_groups_select(experiment_id, org_id=org_id)
    )
    totals = fold_experiment_cost_groups(result.all())
    await _add_experiment_composite_costs(
        session, totals, experiment_id=experiment_id, org_id=org_id
    )
    return totals
