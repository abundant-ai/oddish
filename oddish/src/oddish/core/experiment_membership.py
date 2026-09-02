"""Membership predicate for experiments that gather existing trials.

A collection experiment owns trials additively via ``experiment_trials``
without changing each trial's canonical ``trials.experiment_id``. Read paths
that scope trials to an experiment union both sources through these helpers so
the predicate lives in exactly one place.

Two shapes are offered, and the difference is the query plan:

* :func:`trial_in_experiment` is a WHERE predicate over the whole ``trials``
  table: ``experiment_id = X OR id IN (gathered)``. Postgres cannot serve that
  OR from an index, so any query that applies it to ``trials`` scans the table.
  It remains for callers that already have a ``TrialModel`` in their FROM list
  and add selective predicates of their own (a single trial id, a task id).
* :func:`experiment_trial_scope` turns the same membership into a FROM clause:
  ``TrialModel`` aliased onto ``UNION ALL`` of the two sources, each of which
  is one index seek (``experiment_id = X``; ``experiment_trials.experiment_id
  = X`` joined by primary key). Anything that reads *all* of an experiment's
  trials -- the experiment page, its summary, its cost tiles -- selects from
  ``scope.trials`` so the work is proportional to the experiment, not to the
  table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select, union_all
from sqlalchemy.orm import aliased

from oddish.core.cost_basis import COMBINE_IDEMPOTENCY_PREFIX, not_combine_copy_filter
from oddish.db import experiment_trials
from oddish.db.models import AGENT_TRIAL_KIND, TrialModel


def gathered_trial_ids_select(experiment_id: str) -> Any:
    return (
        select(experiment_trials.c.trial_id)
        .where(experiment_trials.c.experiment_id == experiment_id)
        .where(experiment_trials.c.deleted_at.is_(None))
    )


def combine_source_key(trial: Any, source: Any) -> Any:
    """The idempotency key ``trial`` carries if it is a combine copy of ``source``.

    SQL twin of ``deletion._combine_idempotency_key(trial.experiment_id,
    source.id)``, including its hashed form for long ids.
    """
    prefix = COMBINE_IDEMPOTENCY_PREFIX
    readable_key = literal(prefix) + trial.experiment_id + literal(":") + source.id
    hash_input = (
        func.convert_to(trial.experiment_id, "UTF8")
        .op("||")(func.decode("00", "hex"))
        .op("||")(func.convert_to(source.id, "UTF8"))
    )
    hashed_key = literal(prefix) + func.left(
        func.encode(func.sha256(hash_input), "hex"), 64 - len(prefix)
    )
    return case((func.length(readable_key) <= 64, readable_key), else_=hashed_key)


def not_combine_copy_of_member(trial: Any, experiment_id: str) -> Any:
    """``trial`` is not a combine copy of another member of the experiment.

    A combine re-records an existing execution as a new trial row; when both
    the copy and its source belong to the same experiment the copy is hidden so
    the execution counts once. The source lookup is driven by ``trials.task_id``
    (a copy shares its source's task), so it stays cheap per row whatever
    ``trial`` selects from.
    """
    source = aliased(TrialModel)
    return or_(
        not_combine_copy_filter(trial),
        ~select(1)
        .where(
            source.task_id == trial.task_id,
            trial.idempotency_key == combine_source_key(trial, source),
            or_(
                source.experiment_id == experiment_id,
                source.id.in_(gathered_trial_ids_select(experiment_id)),
            ),
        )
        .correlate(trial)
        .exists(),
    )


def trial_in_experiment(experiment_id: str):
    return and_(
        or_(
            TrialModel.experiment_id == experiment_id,
            TrialModel.id.in_(gathered_trial_ids_select(experiment_id)),
        ),
        not_combine_copy_of_member(TrialModel, experiment_id),
    )


def visible_experiment_trial_predicates(experiment_id: str) -> tuple[Any, ...]:
    """The trial population rendered by member and public experiment grids."""
    return (
        trial_in_experiment(experiment_id),
        TrialModel.deleted_at.is_(None),
        TrialModel.kind == AGENT_TRIAL_KIND,
        TrialModel.is_probe.is_(False),
        TrialModel.superseded_by_trial_id.is_(None),
    )


def experiment_member_trials_select(experiment_id: str, *, org_id: str | None = None):
    """``UNION ALL`` of the experiment's homed and gathered trial rows.

    The gathered branch skips rows already homed here, so a trial that is both
    appears once. ``org_id`` narrows both branches when the caller knows it,
    which lets the homed branch walk ``idx_trials_org_experiment_created_at``
    already ordered by ``created_at``.
    """
    homed = select(TrialModel).where(TrialModel.experiment_id == experiment_id)
    gathered = (
        select(TrialModel)
        .join(experiment_trials, experiment_trials.c.trial_id == TrialModel.id)
        .where(
            experiment_trials.c.experiment_id == experiment_id,
            experiment_trials.c.deleted_at.is_(None),
            TrialModel.experiment_id != experiment_id,
        )
    )
    if org_id is not None:
        homed = homed.where(TrialModel.org_id == org_id)
        gathered = gathered.where(TrialModel.org_id == org_id)
    return union_all(homed, gathered)


@dataclass(frozen=True)
class ExperimentTrialScope:
    """One experiment's trials as a selectable entity.

    ``trials`` behaves like ``TrialModel`` (``scope.trials.task_id`` and so on)
    but its rows are exactly the experiment's members: the UNION ALL of
    :func:`experiment_member_trials_select`, minus combine copies whose source
    is itself a member. Queries that ``select_from(scope.trials)`` never touch
    trials outside the experiment. Build one per statement with
    :func:`experiment_trial_scope`; the same scope may feed several subqueries
    of that statement.
    """

    experiment_id: str
    trials: Any

    def member_predicates(self) -> tuple[Any, ...]:
        """Every trial the experiment counts. Membership already lives in
        ``trials`` itself, so this is empty; kept so call sites read the same
        as :meth:`visible_predicates`."""
        return ()

    def visible_predicates(self) -> tuple[Any, ...]:
        """Grid population: the display filters of
        :func:`visible_experiment_trial_predicates`."""
        trials = self.trials
        return (
            *self.member_predicates(),
            trials.deleted_at.is_(None),
            trials.kind == AGENT_TRIAL_KIND,
            trials.is_probe.is_(False),
            trials.superseded_by_trial_id.is_(None),
        )

    def member_trial_ids_select(self) -> Any:
        """Ids of every member trial, for ``IN`` semi-joins from other tables."""
        return select(self.trials.id).where(*self.member_predicates())


def experiment_trial_scope(
    experiment_id: str, *, org_id: str | None = None
) -> ExperimentTrialScope:
    member = aliased(
        TrialModel,
        experiment_member_trials_select(experiment_id, org_id=org_id).subquery(),
    )
    source = aliased(
        TrialModel,
        experiment_member_trials_select(experiment_id, org_id=org_id).subquery(),
    )
    # Hide a combine copy while its source is also a member, so the execution
    # counts once. Written as an anti-join on ``task_id`` (a copy shares its
    # source's task) rather than a correlated NOT EXISTS: the planner charges a
    # correlated subplan on every row, and that inflated cost is what pushes
    # Postgres into JIT-compiling the whole statement on each request.
    owned = (
        select(member)
        .select_from(member)
        .outerjoin(
            source,
            and_(
                member.idempotency_key.like(f"{COMBINE_IDEMPOTENCY_PREFIX}%"),
                source.task_id == member.task_id,
                member.idempotency_key == combine_source_key(member, source),
            ),
        )
        .where(source.id.is_(None))
        .subquery()
    )
    return ExperimentTrialScope(
        experiment_id=experiment_id, trials=aliased(TrialModel, owned)
    )
