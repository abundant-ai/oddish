from __future__ import annotations

from types import SimpleNamespace

import pytest

from oddish.db import TrialStatus
from oddish.workers.queue import trial_handler


def _trial(**overrides):
    values = {
        "id": "trial-1",
        "environment": "archil",
        "status": TrialStatus.SUCCESS,
        "harbor_stage": "completed",
        "attempts": 2,
        "max_attempts": 3,
        "kind": "agent",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_finished_event_uses_stored_environment_and_attempt_count() -> None:
    outcome = SimpleNamespace(
        phase_timing={
            "environment_setup": {"duration_sec": 42.5},
            "agent_setup": {"duration_sec": 3},
        },
        duration_sec=60.0,
    )

    attributes = trial_handler._trial_execution_finished_attributes(_trial(), outcome)

    assert attributes == {
        "trial_id": "trial-1",
        "kind": "agent",
        "environment": "archil",
        "status": "success",
        "harbor_stage": "completed",
        "attempts": 2,
        "environment_setup_seconds": 42.5,
        "total_seconds": 60.0,
    }


def test_finished_event_labels_analysis_kind_and_worker_environment(monkeypatch) -> None:
    monkeypatch.setattr(trial_handler.settings, "harbor_environment", "daytona")

    attributes = trial_handler._trial_execution_finished_attributes(
        _trial(
            id="audit-1",
            environment=None,
            status=TrialStatus.FAILED,
            harbor_stage="starting",
            kind="audit",
        ),
        None,
    )

    assert attributes["trial_id"] == "audit-1"
    assert attributes["kind"] == "audit"
    assert attributes["environment"] == "daytona"
    assert attributes["status"] == "failed"
    assert attributes["environment_setup_seconds"] is None
    assert attributes["total_seconds"] is None


@pytest.mark.asyncio
async def test_finished_event_is_emitted_after_result_storage_returns(monkeypatch) -> None:
    order = []
    event = {
        "trial_id": "trial-1",
        "kind": "agent",
        "environment": "archil",
        "status": "success",
        "harbor_stage": "completed",
        "attempts": 1,
        "environment_setup_seconds": 2.0,
        "total_seconds": 10.0,
    }

    async def store_results(**_kwargs):
        order.append("storage_returned_after_commit")
        return True, True, event

    async def finish_settlement(**_kwargs):
        order.append("lifecycle_finished")

    monkeypatch.setattr(trial_handler, "_store_trial_results", store_results)
    monkeypatch.setattr(trial_handler, "_finish_trial_settlement", finish_settlement)
    monkeypatch.setattr(
        trial_handler,
        "record_trial_execution_finished",
        lambda **_attributes: order.append("event_emitted"),
    )

    prepared = SimpleNamespace(trial_attempt=1, org_id=None, billed_user_id=None)
    execution = SimpleNamespace(outcome=None, execution_error=None, retryable=True)
    await trial_handler._settle_trial_attempt(
        trial_id="trial-1",
        prepared_trial=prepared,
        execution=execution,
        worker_id=None,
        worker_job_id=None,
    )

    assert order == [
        "storage_returned_after_commit",
        "event_emitted",
        "lifecycle_finished",
    ]
