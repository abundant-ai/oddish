"""Shared settled-cost basis for quota accounting and the admin cost dashboards.

One definition of "what a finished trial costs" so quota enforcement and every
cost view agree (that consistency is the whole point of this module). A settled
trial costs:

* exactly its recorded ``cost_usd`` when present -- this also preserves a
  cancelled trial's real partial spend; otherwise
* ``$0`` when it was cancelled by the user (``harbor_stage == 'cancelled'``) --
  an unpriced cancel is never token-estimated or floored; otherwise
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

from sqlalchemy import and_, case, func

from oddish.config import settings
from oddish.db import TrialModel
from oddish.model_pricing import estimate_cost_usd

# A user cancel stamps trials FAILED + ``harbor_stage='cancelled'`` (see
# ``oddish.queue.CANCELLED_HARBOR_STAGE``); trials have no CANCELLED status, so
# this stage string is the canonical "cancelled" signal on the trial row.
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


def settled_cost_columns() -> list:
    """Labeled SQL columns decomposing settled cost for a model-grouped SELECT.

    Returns ``native_cost`` (summed recorded cost) plus the token sums and trial
    count for the estimatable subset. Reduce the resulting rows (which must also
    carry ``TrialModel.model``) with :func:`settled_cost_parts`.
    """
    estimatable = _estimatable()
    return [
        func.coalesce(
            func.sum(
                case((TrialModel.cost_usd.isnot(None), TrialModel.cost_usd), else_=0.0)
            ),
            0.0,
        ).label("native_cost"),
        func.coalesce(
            func.sum(case((estimatable, TrialModel.input_tokens), else_=0)), 0
        ).label("est_input"),
        func.coalesce(
            func.sum(case((estimatable, TrialModel.output_tokens), else_=0)), 0
        ).label("est_output"),
        func.coalesce(
            func.sum(case((estimatable, TrialModel.cache_tokens), else_=0)), 0
        ).label("est_cache"),
        func.coalesce(func.sum(case((estimatable, 1), else_=0)), 0).label("est_trials"),
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
    )
    if estimated is None:
        estimated = int(getattr(row, "est_trials", 0) or 0) * float(
            settings.unpriced_trial_cost_usd
        )
    return native, estimated


def settled_cost_from_row(row) -> float:
    """Total settled cost (native + estimate) for one model-grouped row."""
    native, estimated = settled_cost_parts(row)
    return native + estimated


def sum_settled_cost(rows: Iterable) -> float:
    """Total settled cost across model-grouped rows."""
    return sum(settled_cost_from_row(row) for row in rows)
