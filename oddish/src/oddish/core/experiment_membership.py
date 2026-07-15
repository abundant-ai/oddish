"""Membership predicate for experiments that gather existing trials.

A collection experiment owns trials additively via ``experiment_trials``
without changing each trial's canonical ``trials.experiment_id``. Read paths
that scope trials to an experiment union both sources through these helpers so
the predicate lives in exactly one place.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from oddish.db import experiment_trials, task_experiments
from oddish.db.models import TrialModel


def gathered_trial_ids_select(experiment_id: str) -> Any:
    return (
        select(experiment_trials.c.trial_id)
        .where(experiment_trials.c.experiment_id == experiment_id)
        .where(experiment_trials.c.deleted_at.is_(None))
    )


def linked_task_ids_select(experiment_id: str) -> Any:
    """Tasks shared into ``experiment_id`` via ``task_experiments`` rows.

    The grid reaches these tasks' trials through the task fan-out rather than
    a trial predicate, which is why ``trial_in_experiment`` doesn't include
    this arm; trial-scoped readers that must match everything the grid renders
    (the cost rollup) add it themselves.
    """
    return (
        select(task_experiments.c.task_id)
        .where(task_experiments.c.experiment_id == experiment_id)
        .where(task_experiments.c.deleted_at.is_(None))
    )


def trial_in_experiment(experiment_id: str):
    """Boolean clause: a trial belongs to ``experiment_id`` either as its home
    (scalar column) or via a gathered membership row."""
    return or_(
        TrialModel.experiment_id == experiment_id,
        TrialModel.id.in_(gathered_trial_ids_select(experiment_id)),
    )
