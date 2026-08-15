"""Admin-declared spend that isn't real, and how to recognise it.

Some spend Oddish records was never actually paid for: provider-sponsored
capacity, free preview tiers, vendor credits, a bake-off an account manager
comped. Operators register it here along two axes -- by **model**
(``cost_excluded_models``) and by **experiment** (``cost_excluded_experiments``)
-- and every accounting surface then agrees to ignore it.

Two consumers, and the split between them is the point of this module:

* **Accounting** (:mod:`oddish.core.cost_basis`) drops excluded spend in SQL,
  via ``first_party_spend_filter``. The admin cost dashboards and quota
  enforcement share that predicate, so an excluded dollar neither appears on a
  cost chart nor counts against a cap.
* **Display** surfaces keep showing the money -- an experiment page still
  prices the work it ran -- but must label it, or a reader reconciling the
  experiment tile against the admin dashboard finds a gap with no explanation.
  :class:`CostExclusions` is the Python-side twin of the SQL predicates for
  exactly that: load it once per request, then ask it about already-loaded
  rows instead of issuing a query per trial.

The two axes are deliberately different shapes. A model exclusion is global
and retroactive: flag ``moonshot/kimi-k2`` and every trial that ever ran on it
stops counting, because the reason it isn't real spend is a property of the
model, not of when it ran. An experiment exclusion is scoped to trials
**homed** in that experiment (``trials.experiment_id``), so a collection that
merely gathers other experiments' trials doesn't launder their cost -- a
gathered trial keeps counting wherever it was really spent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import normalize_model_id
from oddish.db import CostExcludedExperimentModel, CostExcludedModelModel, TrialModel
from oddish.db.pg_errors import is_missing_table

logger = logging.getLogger(__name__)

# The ``cost_exclusion_reason`` values that reach API payloads and the UI.
# Stable strings: the frontend switches on them to pick tooltip copy.
REASON_MODEL = "model"
REASON_EXPERIMENT = "experiment"


def canonical_excluded_model(model: str | None) -> str:
    """The single spelling a model exclusion is stored under.

    Writers canonicalize through this so the live UNIQUE index collapses case
    and whitespace variants into one row. ``normalize_model_id`` is the same
    function ``schemas`` runs over ``trials.model`` on write, so a stored
    exclusion and a stored trial model are always spelled identically -- which
    is what lets the SQL side match with a plain equality against
    ``lower(trim(trials.model))`` instead of reimplementing normalization in
    SQL. Returns ``""`` for input that normalizes away, which callers reject.
    """
    return normalize_model_id(model) or ""


def _excluded_model_spend():
    """Trials whose model admins flagged cost-excluded.

    A correlated EXISTS on the live ``cost_excluded_models`` rows.
    ``model_name`` is stored canonicalized and ``trials.model`` is normalized
    by the same function on write, so plain equality against
    ``lower(trim(model))`` matches without reimplementing normalization in SQL
    (the extra ``lower(trim(...))`` only guards rows written before that
    normalization landed). A NULL ``trials.model`` never matches, so it is
    kept -- exclusion fails open, the same way an unknown provider does.

    The ``deleted_at`` filter is explicit because every cost surface runs with
    ``include_deleted=True``, which disables the session-level soft-delete
    filter -- so removing a model from the list re-includes its spend.
    """
    return (
        select(CostExcludedModelModel.id)
        .where(
            CostExcludedModelModel.model_name
            == func.lower(func.trim(TrialModel.model)),
            CostExcludedModelModel.deleted_at.is_(None),
        )
        .correlate(TrialModel)
        .exists()
    )


def not_excluded_model_filter():
    """Drop trials running a model on the admin cost-exclusion list.

    Shared by ``cost_basis.first_party_spend_filter`` (settled spend) and the
    quota inflight reservation, so excluded-model spend neither counts against
    a cap once settled nor reserves against it while an attempt is running.
    """
    return ~_excluded_model_spend()


def _excluded_experiment_spend():
    """Trials homed in an experiment that admins flagged cost-excluded.

    A correlated EXISTS on the live ``cost_excluded_experiments`` rows,
    matching ``trials.experiment_id`` (the home experiment), so collection
    experiments that merely gather trials are unaffected. The ``deleted_at``
    filter is explicit for the same reason as :func:`_excluded_model_spend`.
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
    """Drop trials homed in an experiment on the admin cost-exclusion list."""
    return ~_excluded_experiment_spend()


def excluded_spend_filter():
    """The complement: trials whose spend admins declared not real.

    For surfaces that need to *measure* excluded spend rather than drop it --
    the experiment cost tile splits its groups on this so it can say how much
    of what it shows doesn't count.
    """
    return _excluded_model_spend() | _excluded_experiment_spend()


@dataclass(frozen=True)
class CostExclusions:
    """A snapshot of both exclusion lists, for annotating loaded rows.

    Python-side twin of ``cost_basis.not_excluded_model_filter`` /
    ``not_excluded_experiment_filter``. Keep the two in step: a surface that
    labels spend differently from the way accounting drops it is worse than
    one that says nothing.
    """

    models: frozenset[str] = field(default_factory=frozenset)
    experiment_ids: frozenset[str] = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        return bool(self.models or self.experiment_ids)

    def reason_for(
        self, *, model: str | None = None, experiment_id: str | None = None
    ) -> str | None:
        """Why this trial's spend isn't real, or ``None`` when it is.

        ``experiment_id`` must be the trial's **home** experiment, matching
        the SQL predicate -- passing the id of a collection the trial was
        merely gathered into would exclude spend the query still counts.

        Model wins over experiment when both apply, so the label names the
        durable reason: the experiment can be un-excluded, but a free model is
        free whatever ran it.
        """
        if model and canonical_excluded_model(model) in self.models:
            return REASON_MODEL
        if experiment_id and experiment_id in self.experiment_ids:
            return REASON_EXPERIMENT
        return None

    def excludes(
        self, *, model: str | None = None, experiment_id: str | None = None
    ) -> bool:
        return self.reason_for(model=model, experiment_id=experiment_id) is not None


async def load_cost_exclusions(session: AsyncSession) -> CostExclusions:
    """Both exclusion lists in one snapshot, for a request to reuse.

    Degrades to an empty snapshot when the tables are missing, so the
    deploy-before-migrate window renders unlabelled spend rather than 500ing
    every page that shows a dollar figure. The savepoint keeps the caller's
    transaction usable after that -- callers run more queries afterwards. Only
    a MISSING table degrades; any other SQL fault must surface.
    """
    try:
        async with session.begin_nested():
            models = list(await session.scalars(select(CostExcludedModelModel)))
            experiments = list(
                await session.scalars(select(CostExcludedExperimentModel))
            )
    except ProgrammingError as exc:
        if not is_missing_table(exc):
            raise
        logger.warning(
            "cost exclusion lists unavailable (schema not migrated yet); "
            "spend is shown unlabelled",
            exc_info=True,
        )
        return CostExclusions()

    return CostExclusions(
        models=frozenset(
            key for row in models if (key := canonical_excluded_model(row.model_name))
        ),
        experiment_ids=frozenset(row.experiment_id for row in experiments),
    )
