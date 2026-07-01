"""Membership predicate for experiments that gather existing trials.

A collection experiment owns trials additively via ``experiment_trials``
without changing each trial's canonical ``trials.experiment_id``. Read paths
that scope trials to an experiment union both sources through these helpers so
the predicate lives in exactly one place.
"""
from __future__ import annotations

from sqlalchemy import Select, or_, select

from oddish.db import experiment_trials
from oddish.db.models import TrialModel


def gathered_trial_ids_select(experiment_id: str) -> Select:
    return (
        select(experiment_trials.c.trial_id)
        .where(experiment_trials.c.experiment_id == experiment_id)
        .where(experiment_trials.c.deleted_at.is_(None))
    )


def trial_in_experiment(experiment_id: str):
    """Boolean clause: a trial belongs to ``experiment_id`` either as its home
    (scalar column) or via a gathered membership row."""
    return or_(
        TrialModel.experiment_id == experiment_id,
        TrialModel.id.in_(gathered_trial_ids_select(experiment_id)),
    )
