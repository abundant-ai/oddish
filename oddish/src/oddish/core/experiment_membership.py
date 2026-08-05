"""Membership predicate for experiments that gather existing trials.

A collection experiment owns trials additively via ``experiment_trials``
without changing each trial's canonical ``trials.experiment_id``. Read paths
that scope trials to an experiment union both sources through these helpers so
the predicate lives in exactly one place.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from oddish.core.cost_basis import not_combine_copy_filter
from oddish.db import experiment_trials
from oddish.db.models import TrialModel


def gathered_trial_ids_select(experiment_id: str) -> Any:
    return (
        select(experiment_trials.c.trial_id)
        .where(experiment_trials.c.experiment_id == experiment_id)
        .where(experiment_trials.c.deleted_at.is_(None))
    )


def trial_in_experiment(experiment_id: str):
    gathered = gathered_trial_ids_select(experiment_id)
    source = aliased(TrialModel)
    return and_(
        or_(
            TrialModel.experiment_id == experiment_id,
            TrialModel.id.in_(gathered),
        ),
        or_(
            not_combine_copy_filter(),
            ~select(1)
            .where(
                source.id == func.split_part(TrialModel.idempotency_key, ":", 3),
                or_(
                    source.experiment_id == experiment_id,
                    source.id.in_(gathered),
                ),
            )
            .correlate(TrialModel)
            .exists(),
        ),
    )
