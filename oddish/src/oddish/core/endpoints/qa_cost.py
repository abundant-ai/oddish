"""QA/analysis spend rolled up per trial, task, and experiment.

Sibling of :mod:`experiment_cost`, which does the same for the solving agent's
spend. "QA cost" means every ``analysis_costs`` row regardless of ``job_kind``
-- the same definition the admin cost breakdown uses -- so the word means one
thing across the app.

**Why these queries join through ``trials`` instead of reading a scope column.**
Attribution on ``analysis_costs`` is asymmetric by producer:

* ``trial_classifier`` (post-trial QA) sets ``trial_id`` and the trial's HOME
  ``experiment_id``, but never ``task_id``.
* Task-level QA sets ``task_id`` and nothing else.

So ``WHERE task_id = :id`` would report ~$0 for a task whose QA is all
per-trial classification, and ``WHERE experiment_id = :id`` would miss a
collection's *gathered* trials, whose rows point at their home experiment.
Membership is not a storable column: the agent-cost tile resolves it with
``trial_in_experiment``, and this must use the same predicate or the two
figures on one tile describe different populations.

Directly-attributed rows are UNIONed in, so a row carrying both ``trial_id``
and ``task_id`` (possible after the attribution fix) is charged once.

Filter parity with ``experiment_cost`` is deliberate: ``include_deleted=True``,
and no version / superseded / probe filtering. Deleting a trial removes it from
the view; it does not refund it. Soft-deleted ``analysis_costs`` rows ARE
excluded -- those are voided charges, not hidden ones. ``analysis_costs`` is
not registered with the soft-delete auto-filter, so that exclusion is spelled
out here rather than inherited.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel
from sqlalchemy import case, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.cost_basis import not_combine_copy_filter
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db.models import (
    AnalysisCostModel,
    ExperimentModel,
    TrialModel,
    task_experiments,
)


class QaCostTotals(BaseModel):
    """QA spend for one scope."""

    qa_cost_usd: float = 0.0
    qa_job_count: int = 0
    qa_has_estimated: bool = False
    qa_has_native: bool = False


class ExperimentQaCostTotals(QaCostTotals):
    """Adds the home-only scope, mirroring ``ExperimentCostTotals.owned_*``.

    ``qa_cost_usd`` covers every member trial (homed or gathered);
    ``owned_qa_cost_usd`` covers only trials homed in the experiment and is the
    number that stays additive across experiments.
    """

    owned_qa_cost_usd: float = 0.0
    owned_qa_job_count: int = 0


_LIVE = AnalysisCostModel.deleted_at.is_(None)
_COST = func.coalesce(AnalysisCostModel.cost_usd, 0.0)

# Row-level projection every scope folds. ``row_id`` is carried so a UNION
# de-duplicates a ledger row reached by more than one attribution path.
_LEDGER_ROW = (
    AnalysisCostModel.id.label("row_id"),
    _COST.label("cost_usd"),
    AnalysisCostModel.cost_source.label("cost_source"),
)


def _fold(rows) -> QaCostTotals:
    """Combine pre-aggregated group rows (``cost_usd``/``job_count``/
    ``has_native``/``has_estimated`` columns from a SQL ``GROUP BY``) into one
    totals object. Folds at most a handful of group rows, never raw ledger
    rows."""
    totals = QaCostTotals()
    for row in rows:
        totals.qa_cost_usd += float(row.cost_usd)
        totals.qa_job_count += row.job_count
        totals.qa_has_native = totals.qa_has_native or bool(row.has_native)
        totals.qa_has_estimated = totals.qa_has_estimated or bool(row.has_estimated)
    return totals


def _group_flags(cost_source):
    """``bool_or`` pair for the native/estimated flags, shared by every
    ``GROUP BY`` query in this module.

    ``cost_source != "native"`` (rather than ``~(cost_source == "native")``)
    matters only defensively -- the column is NOT NULL -- but keeps the
    estimated flag correct if that ever changes: SQL's NULL-propagating
    ``==`` would otherwise drop a NULL row from both flags instead of
    counting it as estimated.
    """
    return (
        func.bool_or(cost_source == "native").label("has_native"),
        func.bool_or(cost_source != "native").label("has_estimated"),
    )


async def get_trial_qa_costs(
    session: AsyncSession,
    *,
    trial_ids: Sequence[str],
    org_id: str | None = None,
) -> dict[str, float]:
    """``trial_id -> QA dollars``, omitting trials with no QA spend.

    Keyed on the page's trial ids rather than joined into the paginated trial
    list query, so that query's plan is untouched. Uses
    ``ix_analysis_costs_trial_id``.

    Trials with no QA are ABSENT rather than zero: the UI renders nothing for
    them, and most trials have no QA.
    """
    if not trial_ids:
        return {}

    query = (
        select(
            AnalysisCostModel.trial_id.label("trial_id"),
            func.sum(_COST).label("cost_usd"),
        )
        .where(_LIVE, AnalysisCostModel.trial_id.in_(list(trial_ids)))
        .group_by(AnalysisCostModel.trial_id)
    )
    if org_id is not None:
        query = query.where(AnalysisCostModel.org_id == org_id)

    return {
        row.trial_id: float(row.cost_usd)
        for row in (await session.execute(query)).all()
    }


async def get_task_qa_costs(
    session: AsyncSession,
    *,
    task_ids: Sequence[str],
    org_id: str | None = None,
    trial_scope_pairs: Sequence[tuple[str, str]] | None = None,
) -> dict[str, QaCostTotals]:
    """``task_id -> QaCostTotals``, omitting tasks with no QA spend.

    A row counts for a task if it is attributed to the task directly OR to any
    trial of that task. The two branches are a real ``UNION``: set semantics
    collapse the ``(row_id, task_id)`` pair a row reached both ways produces,
    so it is charged exactly once, while a row whose ``task_id`` and whose
    trial's task differ still counts once for each -- which a
    ``COALESCE(analysis_costs.task_id, trials.task_id)`` projection could not
    express, and which could otherwise return a task nobody asked for. That
    also means a row can be charged to two different tasks in the same
    result: per-task totals are not additive across a task list, and must not
    be summed into a single "total QA spend" figure.

    The UNION is aggregated in SQL (``GROUP BY`` over the deduped rows), not
    folded row-by-row in Python, so a page of many task ids pulls back at
    most one row per task rather than every ledger row in scope.

    ``trial_scope_pairs`` narrows the per-trial branch to a given set of
    ``(task_id, task_version_id)`` pairs and applies the same
    current-version / non-probe / non-superseded / non-combine-copy / live
    filters the task-browse card uses for its agent-cost figure -- so the two
    numbers on one card describe the same trials. Task-level (``task_id``)
    rows are untouched: they are not version-scoped. When ``None`` (task
    detail, experiment views) the branch stays broad and counts QA on every
    trial regardless of version or soft-delete state.
    """
    if not task_ids:
        return {}

    ids = list(task_ids)
    scoped = trial_scope_pairs is not None
    direct = select(*_LEDGER_ROW, AnalysisCostModel.task_id.label("task_id")).where(
        _LIVE, AnalysisCostModel.task_id.in_(ids)
    )
    via_trials = (
        select(*_LEDGER_ROW, TrialModel.task_id.label("task_id"))
        .select_from(AnalysisCostModel)
        .join(TrialModel, TrialModel.id == AnalysisCostModel.trial_id)
        .where(_LIVE, TrialModel.task_id.in_(ids))
    )
    # QA/audit run as trials now; their spend sits on the trial row, not in
    # the ledger. Like task-level ledger rows they are not version-scoped.
    # The row_id prefix keeps them from colliding with ledger ids in the
    # UNION. Reruns and deletes do not refund spend, so no superseded or
    # soft-delete filters.
    analysis_trials = select(
        func.concat("trial:", TrialModel.id).label("row_id"),
        func.coalesce(TrialModel.cost_usd, 0.0).label("cost_usd"),
        literal("native").label("cost_source"),
        TrialModel.task_id.label("task_id"),
    ).where(
        TrialModel.task_id.in_(ids),
        TrialModel.kind != "agent",
        TrialModel.cost_usd.isnot(None),
    )
    if scoped:
        via_trials = via_trials.where(
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.is_probe.isnot(True),
            not_combine_copy_filter(),
            tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(
                list(trial_scope_pairs)
            ),
        )
    if org_id is not None:
        direct = direct.where(AnalysisCostModel.org_id == org_id)
        via_trials = via_trials.where(AnalysisCostModel.org_id == org_id)
        analysis_trials = analysis_trials.where(TrialModel.org_id == org_id)

    u = direct.union(via_trials, analysis_trials).subquery()
    query = select(
        u.c.task_id,
        func.sum(u.c.cost_usd).label("cost_usd"),
        func.count().label("job_count"),
        *_group_flags(u.c.cost_source),
    ).group_by(u.c.task_id)
    if not scoped:
        # Broad callers count QA on soft-deleted trials too: deleting a trial
        # removes it from the view, it does not refund QA already spent. The
        # scoped card path leaves this off so the soft-delete auto-filter drops
        # those trials, mirroring the card's agent figure.
        query = query.execution_options(include_deleted=True)

    return {row.task_id: _fold([row]) for row in (await session.execute(query)).all()}


async def get_experiment_qa_cost_totals(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> ExperimentQaCostTotals:
    """QA spend over every member trial, plus experiment-level QA jobs.

    ``owned_*`` counts only rows whose trial is HOMED in this experiment, so a
    collection's gathered trials raise ``qa_cost_usd`` but not
    ``owned_qa_cost_usd``. Rows with no trial fall back to their own
    ``experiment_id``: an experiment-level job is owned by the experiment it
    ran for.

    The join is on ``trials.id``, so it matches at most one trial per ledger
    row and cannot fan out -- no ``DISTINCT`` needed.

    Aggregated in SQL, grouped by ``owned``: at most two group rows come back
    (owned / not-owned) rather than every ledger row in scope.
    """
    owned = case(
        (TrialModel.id.isnot(None), TrialModel.experiment_id == experiment_id),
        else_=AnalysisCostModel.experiment_id == experiment_id,
    ).label("owned")

    query = (
        select(
            owned,
            func.sum(_COST).label("cost_usd"),
            func.count().label("job_count"),
            *_group_flags(AnalysisCostModel.cost_source),
        )
        .select_from(AnalysisCostModel)
        .outerjoin(TrialModel, TrialModel.id == AnalysisCostModel.trial_id)
        .where(
            _LIVE,
            or_(
                trial_in_experiment(experiment_id),
                AnalysisCostModel.experiment_id == experiment_id,
            ),
        )
        .group_by(owned)
        .execution_options(include_deleted=True)
    )
    if org_id is not None:
        query = query.where(AnalysisCostModel.org_id == org_id)

    rows = (await session.execute(query)).all()
    base = _fold(rows)
    owned_totals = _fold([row for row in rows if row.owned])

    # QA/audit trial spend. Member scope: analysis trials of any task with
    # live membership here. Owned: the ones homed in this experiment's
    # qa-report shadow (a task in two experiments has its QA in the first
    # one's shadow only).
    shadow_ids = (
        select(ExperimentModel.id)
        .where(ExperimentModel.shadow_of == experiment_id)
        .scalar_subquery()
    )
    trial_query = (
        select(
            TrialModel.experiment_id.in_(shadow_ids).label("owned"),
            func.sum(func.coalesce(TrialModel.cost_usd, 0.0)).label("cost_usd"),
            func.count().label("job_count"),
        )
        .select_from(TrialModel)
        .join(
            task_experiments,
            (task_experiments.c.task_id == TrialModel.task_id)
            & (task_experiments.c.experiment_id == experiment_id)
            & task_experiments.c.deleted_at.is_(None),
        )
        .where(TrialModel.kind != "agent", TrialModel.cost_usd.isnot(None))
        .group_by("owned")
        .execution_options(include_deleted=True)
    )
    if org_id is not None:
        trial_query = trial_query.where(TrialModel.org_id == org_id)
    for row in (await session.execute(trial_query)).all():
        base.qa_cost_usd += float(row.cost_usd)
        base.qa_job_count += row.job_count
        base.qa_has_native = True
        if row.owned:
            owned_totals.qa_cost_usd += float(row.cost_usd)
            owned_totals.qa_job_count += row.job_count

    return ExperimentQaCostTotals(
        **base.model_dump(),
        owned_qa_cost_usd=owned_totals.qa_cost_usd,
        owned_qa_job_count=owned_totals.qa_job_count,
    )
