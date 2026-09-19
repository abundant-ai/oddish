"""CUA / verifier LLM spend rolled up per trial, task, and experiment.

Sibling of :mod:`qa_cost`. Never folds into ``trials.cost_usd``,
``analysis_spend``, or user quotas — ``verifier_costs.billed_user_id`` is
always null. Soft-deleted ledger rows are excluded; soft-deleted trials still
count (deleting a trial does not refund CUA spend already incurred).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel
from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.cost_basis import not_combine_copy_filter
from oddish.core.experiment_membership import experiment_trial_scope
from oddish.db.models import TrialModel, VerifierCostModel

_LIVE = VerifierCostModel.deleted_at.is_(None)
_COST = func.coalesce(VerifierCostModel.cost_usd, 0.0)


class VerifierCostTotals(BaseModel):
    """Verifier LLM spend for one scope."""

    verifier_cost_usd: float = 0.0
    verifier_row_count: int = 0
    verifier_has_estimated: bool = False
    verifier_has_native: bool = False


class ExperimentVerifierCostTotals(VerifierCostTotals):
    """Adds home-only scope, mirroring ``ExperimentQaCostTotals.owned_*``."""

    owned_verifier_cost_usd: float = 0.0
    owned_verifier_row_count: int = 0


def _fold(rows) -> VerifierCostTotals:
    totals = VerifierCostTotals()
    for row in rows:
        totals.verifier_cost_usd += float(row.cost_usd)
        totals.verifier_row_count += int(row.row_count)
        totals.verifier_has_native = totals.verifier_has_native or bool(row.has_native)
        totals.verifier_has_estimated = totals.verifier_has_estimated or bool(
            row.has_estimated
        )
    return totals


def _group_flags(cost_source):
    # Only ``estimated`` means the tile should say "includes estimated".
    # ``backfill`` preserves native/estimated drafts via override but is not
    # itself an estimate flag; null-cost sentinels also use backfill.
    return (
        func.bool_or(cost_source == "native").label("has_native"),
        func.bool_or(cost_source == "estimated").label("has_estimated"),
    )


async def get_trial_verifier_costs(
    session: AsyncSession,
    *,
    trial_ids: Sequence[str],
    org_id: str | None = None,
) -> dict[str, float]:
    """``trial_id -> verifier dollars``, omitting trials with no spend.

    Trials with no CUA are ABSENT rather than zero so the UI renders nothing.
    Null-cost backfill sentinels (``no_cua_artifacts``) are omitted.
    """
    if not trial_ids:
        return {}

    query = (
        select(
            VerifierCostModel.trial_id.label("trial_id"),
            func.sum(_COST).label("cost_usd"),
        )
        .where(
            _LIVE,
            VerifierCostModel.trial_id.in_(list(trial_ids)),
            VerifierCostModel.cost_usd.isnot(None),
        )
        .group_by(VerifierCostModel.trial_id)
    )
    if org_id is not None:
        query = query.where(VerifierCostModel.org_id == org_id)

    return {
        row.trial_id: float(row.cost_usd)
        for row in (await session.execute(query)).all()
        if float(row.cost_usd) > 0
    }


async def get_task_verifier_costs(
    session: AsyncSession,
    *,
    task_ids: Sequence[str],
    org_id: str | None = None,
    trial_scope_pairs: Sequence[tuple[str, str]] | None = None,
) -> dict[str, VerifierCostTotals]:
    """``task_id -> VerifierCostTotals``, omitting tasks with no verifier spend.

    Counts ledger rows attributed to the task directly or via a trial of that
    task. ``trial_scope_pairs`` narrows to the same current-version / non-probe
    population as agent and QA browse figures; when set, only the trial branch
    is used (verifier rows always have ``trial_id``, so an unscoped task_id
    branch would ignore those filters).
    """
    if not task_ids:
        return {}

    ids = list(task_ids)
    scoped = trial_scope_pairs is not None
    ledger = (
        VerifierCostModel.id.label("row_id"),
        _COST.label("cost_usd"),
        VerifierCostModel.cost_source.label("cost_source"),
    )
    # Unlike analysis_costs, every verifier row has a trial_id (and usually
    # task_id). An unscoped direct task_id branch would ignore probe /
    # superseded / version filters when ``trial_scope_pairs`` is set, so the
    # browse card only walks trials.
    via_trials = (
        select(*ledger, TrialModel.task_id.label("task_id"))
        .select_from(VerifierCostModel)
        .join(TrialModel, TrialModel.id == VerifierCostModel.trial_id)
        .where(
            _LIVE,
            TrialModel.task_id.in_(ids),
            VerifierCostModel.cost_usd.isnot(None),
        )
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
            via_trials = via_trials.where(VerifierCostModel.org_id == org_id)
        u = via_trials.subquery()
    else:
        direct = select(*ledger, VerifierCostModel.task_id.label("task_id")).where(
            _LIVE,
            VerifierCostModel.task_id.in_(ids),
            VerifierCostModel.cost_usd.isnot(None),
        )
        if org_id is not None:
            direct = direct.where(VerifierCostModel.org_id == org_id)
            via_trials = via_trials.where(VerifierCostModel.org_id == org_id)
        u = direct.union(via_trials).subquery()
    query = select(
        u.c.task_id,
        func.sum(u.c.cost_usd).label("cost_usd"),
        func.count().label("row_count"),
        *_group_flags(u.c.cost_source),
    ).group_by(u.c.task_id)
    if not scoped:
        query = query.execution_options(include_deleted=True)

    return {
        row.task_id: _fold([row]) for row in (await session.execute(query)).all()
    }


async def get_experiment_verifier_cost_totals(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> ExperimentVerifierCostTotals:
    """Verifier spend over every member trial.

    ``owned_*`` covers only rows whose trial is HOMED in this experiment.
    Soft-deleted member trials still count.
    """
    owned = case(
        (TrialModel.id.isnot(None), TrialModel.experiment_id == experiment_id),
        else_=VerifierCostModel.experiment_id == experiment_id,
    ).label("owned")

    query = (
        select(
            owned,
            func.sum(_COST).label("cost_usd"),
            func.count().label("row_count"),
            *_group_flags(VerifierCostModel.cost_source),
        )
        .select_from(VerifierCostModel)
        .outerjoin(TrialModel, TrialModel.id == VerifierCostModel.trial_id)
        .where(
            _LIVE,
            VerifierCostModel.cost_usd.isnot(None),
            or_(
                VerifierCostModel.trial_id.in_(
                    experiment_trial_scope(
                        experiment_id, org_id=org_id
                    ).member_trial_ids_select()
                ),
                VerifierCostModel.experiment_id == experiment_id,
            ),
        )
        .group_by(owned)
        .execution_options(include_deleted=True)
    )
    if org_id is not None:
        query = query.where(VerifierCostModel.org_id == org_id)

    rows = (await session.execute(query)).all()
    base = _fold(rows)
    owned_totals = _fold([row for row in rows if row.owned])
    return ExperimentVerifierCostTotals(
        **base.model_dump(),
        owned_verifier_cost_usd=owned_totals.verifier_cost_usd,
        owned_verifier_row_count=owned_totals.verifier_row_count,
    )
