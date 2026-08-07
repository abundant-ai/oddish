"""Membership predicate for experiments that gather existing trials.

A collection experiment owns trials additively via ``experiment_trials``
without changing each trial's canonical ``trials.experiment_id``. Read paths
that scope trials to an experiment union both sources through these helpers so
the predicate lives in exactly one place.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import aliased

from oddish.core.cost_basis import COMBINE_IDEMPOTENCY_PREFIX, not_combine_copy_filter
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
    # SQL twin of deletion._combine_idempotency_key, including its hashed form.
    prefix = COMBINE_IDEMPOTENCY_PREFIX
    readable_key = literal(prefix) + TrialModel.experiment_id + literal(":") + source.id
    hash_input = (
        func.convert_to(TrialModel.experiment_id, "UTF8")
        .op("||")(func.decode("00", "hex"))
        .op("||")(func.convert_to(source.id, "UTF8"))
    )
    hashed_key = literal(prefix) + func.left(
        func.encode(func.sha256(hash_input), "hex"), 64 - len(prefix)
    )
    source_key = case((func.length(readable_key) <= 64, readable_key), else_=hashed_key)
    return and_(
        or_(
            TrialModel.experiment_id == experiment_id,
            TrialModel.id.in_(gathered),
        ),
        or_(
            not_combine_copy_filter(),
            ~select(1)
            .where(
                source.task_id == TrialModel.task_id,
                TrialModel.idempotency_key == source_key,
                or_(
                    source.experiment_id == experiment_id,
                    source.id.in_(gathered),
                ),
            )
            .correlate(TrialModel)
            .exists(),
        ),
    )
