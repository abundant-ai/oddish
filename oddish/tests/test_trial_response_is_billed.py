from datetime import datetime

from oddish.core.helpers import (
    build_compact_trial_response,
    build_slim_trial_response,
    build_trial_response,
)
from oddish.db import TrialOrigin, TrialStatus
from oddish.db.models import TrialModel


def _trial(billed_user_id: str | None) -> TrialModel:
    # Set the non-nullable columns whose server/Python defaults only apply on
    # flush (not on in-memory construction), so the mapper's TrialResponse
    # validates without a DB round-trip.
    return TrialModel(
        id="t-0",
        name="t-0",
        task_id="task",
        agent="nop",
        provider="nop_oracle",
        queue_key="nop_oracle",
        model=None,
        status=TrialStatus.SUCCESS,
        attempts=1,
        max_attempts=6,
        origin=TrialOrigin.ODDISH,
        is_probe=False,
        has_trajectory=False,
        billed_user_id=billed_user_id,
        created_at=datetime(2026, 1, 1),
    )


def test_mappers_mark_billed_trials():
    billed = _trial(billed_user_id="user-1")
    assert build_trial_response(billed, task_path="p").is_billed is True
    assert build_compact_trial_response(billed, task_path="p").is_billed is True
    assert build_slim_trial_response(billed, task_path="p").is_billed is True


def test_mappers_mark_unbilled_trials():
    unbilled = _trial(billed_user_id=None)
    assert build_trial_response(unbilled, task_path="p").is_billed is False
    assert build_compact_trial_response(unbilled, task_path="p").is_billed is False
    assert build_slim_trial_response(unbilled, task_path="p").is_billed is False


def test_slim_mapper_includes_input_and_output_tokens():
    trial = _trial(billed_user_id=None)
    trial.input_tokens = 123
    trial.output_tokens = 45

    response = build_slim_trial_response(trial, task_path="p")

    assert response.input_tokens == 123
    assert response.output_tokens == 45
