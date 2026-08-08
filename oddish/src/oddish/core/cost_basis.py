"""Shared settled-cost basis for quota accounting and the admin cost dashboards.

One definition of "what a finished trial costs" so quota enforcement and every
cost view agree (that consistency is the whole point of this module). A settled
trial costs:

* exactly its recorded ``cost_usd`` when present -- this also preserves a
  cancelled trial's real partial spend; otherwise
* ``$0`` when it was cancelled (``harbor_stage == 'cancelled'``) with no
  recorded cost -- a cancel never reaches token settlement, so this only keeps
  the unpriced floor from charging abandoned runs; otherwise
* a LiteLLM token estimate from its input/output/cache tokens, which is ``$0``
  when no tokens were recorded (the unpriced fallback is
  ``settings.unpriced_trial_cost_usd``, ``$0`` by default).

Because ``estimate_cost_usd`` is a Python/LiteLLM lookup and not expressible in
SQL, callers must ``GROUP BY TrialModel.model`` (alongside their own grouping
keys), select ``*settled_cost_columns()`` plus ``TrialModel.model``, and fold
each row through ``settled_cost_parts`` / ``settled_cost_from_row`` /
``sum_settled_cost``.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, case, func, or_, select

from oddish.config import settings
from oddish.db import (
    CostExcludedExperimentModel,
    CostExcludedLlmKeyModel,
    TrialModel,
    TrialOrigin,
)
from oddish.model_pricing import estimate_cost_usd

# ``harbor_stage='cancelled'`` marks an abandoned trial. Three paths stamp it:
# a user cancel (``oddish.queue.cancel_tasks_runs``), the stale-heartbeat /
# orphan reaper (``workers.queue.cleanup``), and a runtime CANCEL event
# (``workers.queue.trial_handler``). A late result may still settle the cancelled
# attempt's tokens and cost, but it cannot replace the cancellation outcome.
# Trials have no CANCELLED status, so this stage remains the canonical
# "cancelled" signal on the row.
CANCELLED_HARBOR_STAGE = "cancelled"

# ``combine_experiments_core`` materializes a source trial as a brand-new row
# under the *same* task, so a copy is indistinguishable from an original except
# for this prefix on ``idempotency_key``. It is the only provenance a copy
# carries -- there is no ``combined_from_trial_id`` column.
COMBINE_IDEMPOTENCY_PREFIX = "combine:"


def is_combine_copy(trial) -> bool:
    """True when this trial row is an experiment-combine materialization.

    Python-side twin of :func:`not_combine_copy_filter`, for surfaces that
    filter an already-loaded relationship instead of issuing a query.
    """
    key = getattr(trial, "idempotency_key", None)
    return bool(key) and key.startswith(COMBINE_IDEMPOTENCY_PREFIX)


def not_combine_copy_filter():
    """Drop experiment-combine copies (idempotency_key ``combine:%``).

    A combine copy re-materializes an existing trial's result under another
    experiment, so counting it double-counts the same execution. Shared by
    :func:`first_party_spend_filter` and the task browser so both count the
    same execution population the task-detail view shows.
    """
    return or_(
        TrialModel.idempotency_key.is_(None),
        TrialModel.idempotency_key.notlike(f"{COMBINE_IDEMPOTENCY_PREFIX}%"),
    )


def _cost_excluded_key_spend():
    """Trials stamped with an LLM key that admins flagged cost-excluded.

    A correlated EXISTS on the live ``cost_excluded_llm_keys`` rows. A NULL
    ``llm_key_hash`` (pre-rollout / unresolved) never matches, so it is kept.
    The ``deleted_at`` filter is explicit because every cost surface runs with
    ``include_deleted=True``, which disables the session-level soft-delete
    filter -- so removing a key from the list re-includes its spend.
    """
    return (
        select(CostExcludedLlmKeyModel.id)
        .where(
            CostExcludedLlmKeyModel.key_hash == TrialModel.llm_key_hash,
            CostExcludedLlmKeyModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .exists()
    )


def not_excluded_llm_key_filter():
    """Drop trials stamped with an LLM key on the admin cost-exclusion list.

    Shared by :func:`first_party_spend_filter` (settled spend) and the quota
    inflight reservation, so excluded-key spend neither counts against a cap
    once settled nor reserves against it while a stamped attempt is retrying.
    """
    return ~_cost_excluded_key_spend()


def _cost_excluded_experiment_spend():
    """Trials homed in an experiment that admins flagged cost-excluded.

    A correlated EXISTS on the live ``cost_excluded_experiments`` rows,
    matching ``trials.experiment_id`` (the home experiment), so collection
    experiments that merely gather trials are unaffected. The ``deleted_at``
    filter is explicit for the same reason as :func:`_cost_excluded_key_spend`:
    cost surfaces run with ``include_deleted=True``, and removing an experiment
    from the list must re-include its spend.
    """
    return (
        select(CostExcludedExperimentModel.id)
        .where(
            CostExcludedExperimentModel.experiment_id == TrialModel.experiment_id,
            CostExcludedExperimentModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .exists()
    )


def not_excluded_experiment_filter():
    """Drop trials homed in an experiment on the admin cost-exclusion list.

    Shared by :func:`first_party_spend_filter` (settled spend) and the quota
    inflight reservation, mirroring :func:`not_excluded_llm_key_filter`.
    """
    return ~_cost_excluded_experiment_spend()


def first_party_spend_filter():
    """Select actual Oddish executions, excluding non-spend materializations.

    Imported trials were paid for outside Oddish. Experiment-combine rows copy
    an existing trial's result and cost, so counting them would charge the same
    execution twice. Spend stamped with an LLM key on the admin cost-exclusion
    list (sponsored/free keys), or homed in an admin cost-excluded experiment,
    is deliberately not counted. Keep this eligibility rule shared by quota
    accounting and cost reporting so both surfaces count the same execution
    population.
    """
    return and_(
        TrialModel.origin == TrialOrigin.ODDISH,
        not_combine_copy_filter(),
        not_excluded_llm_key_filter(),
        not_excluded_experiment_filter(),
    )


def _estimatable():
    """Rows we token-estimate: unpriced (cost_usd NULL) and not user-cancelled.

    ``COALESCE`` first so a NULL ``harbor_stage`` (the normal case) is treated as
    not-cancelled rather than making the comparison NULL and dropping the row.
    """
    return and_(
        TrialModel.cost_usd.is_(None),
        func.coalesce(TrialModel.harbor_stage, "") != CANCELLED_HARBOR_STAGE,
    )


def _has_estimatable_tokens():
    """Rows whose token fields can produce a per-trial estimate."""
    return and_(
        _estimatable(),
        or_(
            func.coalesce(TrialModel.input_tokens, 0) > 0,
            func.coalesce(TrialModel.output_tokens, 0) > 0,
            func.coalesce(TrialModel.cache_write_tokens, 0) > 0,
        ),
    )


def settled_cost_columns() -> list:
    """Labeled SQL columns decomposing settled cost for a model-grouped SELECT.

    Returns ``native_cost`` (summed recorded cost) plus the token sums and trial
    count for the estimatable subset. Reduce the resulting rows (which must also
    carry ``TrialModel.model``) with :func:`settled_cost_parts`.
    """
    estimatable = _estimatable()
    has_tokens = _has_estimatable_tokens()
    nonnegative_input = func.greatest(func.coalesce(TrialModel.input_tokens, 0), 0)
    nonnegative_output = func.greatest(func.coalesce(TrialModel.output_tokens, 0), 0)
    nonnegative_cache = func.greatest(func.coalesce(TrialModel.cache_tokens, 0), 0)
    nonnegative_cache_write = func.greatest(
        func.coalesce(TrialModel.cache_write_tokens, 0), 0
    )
    uncached_input = func.greatest(
        nonnegative_input - nonnegative_cache - nonnegative_cache_write,
        0,
    )
    # ``estimate_cost_usd`` decomposes input into uncached/cache-read/cache-write
    # after aggregating. Recompose each trial first so its per-trial clamp is
    # preserved when many rows are grouped: sum(max(input-cache-cache_write, 0))
    # is not generally equal to
    # max(sum(input)-sum(cache)-sum(cache_write), 0).
    estimator_input = uncached_input + nonnegative_cache + nonnegative_cache_write
    return [
        func.coalesce(
            func.sum(
                case((TrialModel.cost_usd.isnot(None), TrialModel.cost_usd), else_=0.0)
            ),
            0.0,
        ).label("native_cost"),
        func.coalesce(func.sum(case((has_tokens, estimator_input), else_=0)), 0).label(
            "est_input"
        ),
        func.coalesce(
            func.sum(case((has_tokens, nonnegative_output), else_=0)), 0
        ).label("est_output"),
        func.coalesce(
            func.sum(case((has_tokens, nonnegative_cache), else_=0)), 0
        ).label("est_cache"),
        func.coalesce(
            func.sum(case((has_tokens, nonnegative_cache_write), else_=0)), 0
        ).label("est_cache_write"),
        func.coalesce(func.sum(case((estimatable, 1), else_=0)), 0).label("est_trials"),
        func.coalesce(func.sum(case((has_tokens, 1), else_=0)), 0).label(
            "est_token_trials"
        ),
    ]


def settled_cost_parts(row) -> tuple[float, float]:
    """``(native_cost, estimated_cost)`` for one model-grouped row.

    The estimate is the token-derived cost; when tokens/pricing yield nothing it
    falls back to ``unpriced_trial_cost_usd`` per estimatable trial in the group
    ($0 by default, so unpriced runs are free unless the floor is re-enabled).
    """
    native = float(row.native_cost or 0.0)
    estimated = estimate_cost_usd(
        row.model,
        int(row.est_input or 0),
        int(row.est_output or 0),
        int(row.est_cache or 0),
        int(row.est_cache_write or 0),
    )
    estimatable_trials = int(row.est_trials or 0)
    if estimated is None:
        estimated = 0.0
        fallback_trials = estimatable_trials
    else:
        # Aggregating by model is valid for the linear token rates, but fallback
        # pricing is per trial. Keep charging tokenless siblings even when other
        # trials in the same model group produced an estimate.
        fallback_trials = max(
            estimatable_trials - int(row.est_token_trials or 0),
            0,
        )
    estimated += fallback_trials * float(settings.unpriced_trial_cost_usd)
    return native, estimated


def settled_cost_from_row(row) -> float:
    """Total settled cost (native + estimate) for one model-grouped row."""
    native, estimated = settled_cost_parts(row)
    return native + estimated


def sum_settled_cost(rows: Iterable) -> float:
    """Total settled cost across model-grouped rows."""
    return sum(settled_cost_from_row(row) for row in rows)
