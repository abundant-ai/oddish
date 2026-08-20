from datetime import datetime

from oddish.core.cost_exclusions import REASON_EXPERIMENT, REASON_MODEL, CostExclusions
from oddish.core.helpers import (
    build_compact_trial_response,
    build_slim_trial_response,
    build_trial_response,
)
from oddish.db import TrialOrigin, TrialStatus
from oddish.db.models import TrialModel


def _trial(*, model: str | None = None, experiment_id: str | None = None) -> TrialModel:
    return TrialModel(
        id="t-0",
        name="t-0",
        task_id="task",
        agent="nop",
        provider="nop_oracle",
        queue_key="nop_oracle",
        model=model,
        experiment_id=experiment_id,
        status=TrialStatus.SUCCESS,
        attempts=1,
        max_attempts=6,
        origin=TrialOrigin.ODDISH,
        is_probe=False,
        has_trajectory=False,
        created_at=datetime(2026, 1, 1),
    )


def test_table_views_label_excluded_model_family_spend():
    trial = _trial(model="azure/grok-free-preview")
    exclusions = CostExclusions(models=frozenset({"xai/grok-free-preview"}))

    for build in (
        build_trial_response,
        build_compact_trial_response,
        build_slim_trial_response,
    ):
        response = build(trial, task_path="p", exclusions=exclusions)
        assert response.cost_exclusion_reason == REASON_MODEL


def test_table_views_label_excluded_experiment_spend():
    trial = _trial(model="xai/grok-4", experiment_id="exp_1")
    exclusions = CostExclusions(experiment_ids=frozenset({"exp_1"}))

    for build in (
        build_trial_response,
        build_compact_trial_response,
        build_slim_trial_response,
    ):
        response = build(trial, task_path="p", exclusions=exclusions)
        assert response.cost_exclusion_reason == REASON_EXPERIMENT


def test_table_views_leave_real_spend_unlabelled():
    trial = _trial(model="xai/grok-4", experiment_id="exp_2")
    exclusions = CostExclusions(models=frozenset({"xai/grok-free-preview"}))

    for build in (
        build_trial_response,
        build_compact_trial_response,
        build_slim_trial_response,
    ):
        assert (
            build(trial, task_path="p", exclusions=exclusions).cost_exclusion_reason
            is None
        )


def test_callers_that_resolve_no_exclusions_label_nothing():
    trial = _trial(model="xai/grok-free-preview")

    for build in (
        build_trial_response,
        build_compact_trial_response,
        build_slim_trial_response,
    ):
        assert build(trial, task_path="p").cost_exclusion_reason is None
