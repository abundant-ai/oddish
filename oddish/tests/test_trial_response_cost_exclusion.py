from datetime import datetime
from functools import partial

import pytest

from oddish.core.cost_exclusions import (
    REASON_EXPERIMENT,
    REASON_KEY,
    REASON_MODEL,
    CostExclusions,
)
from oddish.core.helpers import (
    build_compact_trial_response,
    build_slim_trial_response,
    build_trial_response,
)
from oddish.db import TrialOrigin, TrialStatus
from oddish.db.models import TrialModel


@pytest.fixture(
    params=[
        build_trial_response,
        build_compact_trial_response,
        partial(build_slim_trial_response, analysis=None, error_message=None),
    ],
    ids=["full", "compact", "slim"],
)
def build(request):
    return request.param


def _trial(
    *,
    llm_key_hash: str | None = None,
    model: str | None = None,
    experiment_id: str | None = None,
) -> TrialModel:
    return TrialModel(
        id="t-0",
        name="t-0",
        task_id="task",
        agent="nop",
        provider="nop_oracle",
        queue_key="nop_oracle",
        model=model,
        llm_key_hash=llm_key_hash,
        experiment_id=experiment_id,
        status=TrialStatus.SUCCESS,
        attempts=1,
        max_attempts=6,
        origin=TrialOrigin.ODDISH,
        is_probe=False,
        has_trajectory=False,
        created_at=datetime(2026, 1, 1),
    )


def test_table_views_label_excluded_model_family_spend(build):
    trial = _trial(model="azure/grok-free-preview")
    exclusions = CostExclusions(models=frozenset({"xai/grok-free-preview"}))

    response = build(trial, task_path="p", exclusions=exclusions)
    assert response.cost_exclusion_reason == REASON_MODEL


def test_table_views_label_excluded_experiment_spend(build):
    trial = _trial(model="xai/grok-4", experiment_id="exp_1")
    exclusions = CostExclusions(experiment_ids=frozenset({"exp_1"}))

    response = build(trial, task_path="p", exclusions=exclusions)
    assert response.cost_exclusion_reason == REASON_EXPERIMENT


def test_table_views_label_preserved_key_exclusions(build):
    trial = _trial(model="xai/grok-4", llm_key_hash="sponsored-key")
    exclusions = CostExclusions(llm_key_hashes=frozenset({"sponsored-key"}))

    response = build(trial, task_path="p", exclusions=exclusions)
    assert response.cost_exclusion_reason == REASON_KEY


def test_table_views_leave_real_spend_unlabelled(build):
    trial = _trial(model="xai/grok-4", experiment_id="exp_2")
    exclusions = CostExclusions(models=frozenset({"xai/grok-free-preview"}))

    assert (
        build(trial, task_path="p", exclusions=exclusions).cost_exclusion_reason is None
    )


def test_callers_that_resolve_no_exclusions_label_nothing(build):
    trial = _trial(model="xai/grok-free-preview")

    assert build(trial, task_path="p").cost_exclusion_reason is None
