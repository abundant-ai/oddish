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

from sqlalchemy import and_, case, func, or_

from oddish.config import settings
from oddish.db import TrialModel
from oddish.model_pricing import estimate_cost_usd

# ``harbor_stage='cancelled'`` marks an abandoned trial. Three paths stamp it:
# a user cancel (``oddish.queue.CANCELLED_HARBOR_STAGE``), the stale-heartbeat /
# orphan reaper (``workers.queue.cleanup``), and a runtime CANCEL event
# (``workers.queue.trial_handler``). None reach the settlement block that writes
# input/output/cache tokens and ``cost_usd`` together, so a cancelled row has no
# tokens -- its token estimate is already $0. Gating it out therefore changes
# nothing unless the unpriced floor is re-enabled, where excluding abandoned
# runs is the intent. Trials have no CANCELLED status, so this stage is the
# canonical "cancelled" signal on the row.
CANCELLED_HARBOR_STAGE = "cancelled"


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
    return [
        func.coalesce(
            func.sum(
                case((TrialModel.cost_usd.isnot(None), TrialModel.cost_usd), else_=0.0)
            ),
            0.0,
        ).label("native_cost"),
        func.coalesce(
            func.sum(case((has_tokens, TrialModel.input_tokens), else_=0)), 0
        ).label("est_input"),
        func.coalesce(
            func.sum(case((has_tokens, TrialModel.output_tokens), else_=0)), 0
        ).label("est_output"),
        func.coalesce(
            func.sum(case((has_tokens, TrialModel.cache_tokens), else_=0)), 0
        ).label("est_cache"),
        func.coalesce(
            func.sum(case((has_tokens, TrialModel.cache_write_tokens), else_=0)), 0
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
