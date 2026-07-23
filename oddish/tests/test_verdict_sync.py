from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.analyze import Classification, TrialClassification
from oddish.core.verdict_sync import build_verdict_payload, sync_verdict_to_task
from oddish.db import TaskStatus, VerdictStatus


class _Verdict:
    is_good = False
    confidence = "high"
    primary_issue = "reward hacking"
    recommendations = ["randomize the fixture"]
    reasoning = "two trials hardcoded the answer"


def _c(classification: Classification) -> TrialClassification:
    return TrialClassification(
        trial_name="t",
        classification=classification,
        subtype="Hardcoding",
        evidence="e",
        root_cause="r",
        recommendation="rec",
    )


def test_payload_shape_and_counts():
    classifications = [
        _c(Classification.BAD_SUCCESS),
        _c(Classification.BAD_FAILURE),
        _c(Classification.GOOD_FAILURE),
        _c(Classification.GOOD_SUCCESS),
        _c(Classification.HARNESS_ERROR),
    ]
    payload = build_verdict_payload(_Verdict(), classifications)

    assert payload == {
        "is_good": False,
        "confidence": "high",
        "primary_issue": "reward hacking",
        "reasoning": "two trials hardcoded the answer",
        "recommendations": ["randomize the fixture"],
        "task_problem_count": 2,  # BAD_SUCCESS + BAD_FAILURE
        "agent_problem_count": 1,  # GOOD_FAILURE
        "success_count": 2,  # GOOD_SUCCESS + BAD_SUCCESS
        "harness_error_count": 1,  # HARNESS_ERROR
    }


def test_counts_ignore_model_supplied_values():
    """Counts come from classifications only -- a model cannot inflate them."""

    class _Lying(_Verdict):
        task_problem_count = 99

    payload = build_verdict_payload(_Lying(), [_c(Classification.GOOD_SUCCESS)])
    assert payload["task_problem_count"] == 0


@pytest.mark.asyncio
async def test_sync_stores_verdict_but_keeps_task_running_when_trials_remain(
    monkeypatch,
):
    task = SimpleNamespace(
        verdict=None,
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
        verdict_finished_at=None,
        status=TaskStatus.VERDICT_PENDING,
        finished_at=None,
    )

    class _Session:
        async def get(self, _model, _task_id, **_kwargs):
            return task

    @asynccontextmanager
    async def fake_get_session():
        yield _Session()

    async def should_complete(_session):
        return False

    monkeypatch.setattr("oddish.core.verdict_sync.get_session", fake_get_session)

    status = await sync_verdict_to_task(
        "task-1",
        payload={"is_good": True, "task_version_id": "task-1-v2"},
        error=None,
        should_complete=should_complete,
    )

    assert status == VerdictStatus.SUCCESS.value
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.RUNNING
    assert task.finished_at is None
