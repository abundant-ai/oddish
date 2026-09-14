from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from oddish.core.verdict_state import (
    abandon_verdict,
    cancel_verdict,
    complete_verdict,
    complete_verdict_without_result,
    fail_verdict,
    queue_verdict,
)
from oddish.db import VerdictStatus


def _task(
    *,
    payload: dict | None = None,
    status: VerdictStatus | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        verdict=payload,
        verdict_status=status,
        verdict_error="old error",
        verdict_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        verdict_finished_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "active", [VerdictStatus.PENDING, VerdictStatus.QUEUED, VerdictStatus.RUNNING]
)
def test_abandon_active_replacement_discards_published_verdict(
    active: VerdictStatus,
) -> None:
    payload = {"verdict": "accept", "is_good": True}
    task = _task(payload=payload, status=active)

    abandon_verdict(task)

    assert task.verdict is None
    assert task.verdict_status is None
    assert task.verdict_error is None
    assert task.verdict_started_at is None
    assert task.verdict_finished_at is None


def test_abandon_first_qa_attempt_returns_to_unstarted_state() -> None:
    task = _task(status=VerdictStatus.RUNNING)

    abandon_verdict(task)

    assert task.verdict is None
    assert task.verdict_status is None
    assert task.verdict_error is None
    assert task.verdict_started_at is None
    assert task.verdict_finished_at is None


@pytest.mark.parametrize("previous", ["accept", "reject", None])
def test_replacement_lifecycle_withdraws_old_result_when_queued(previous) -> None:
    original = (
        {"verdict": previous, "is_good": previous == "accept"} if previous else None
    )
    replacement = {"verdict": "accept", "is_good": True}
    task = _task(payload=original, status=VerdictStatus.SUCCESS)
    finished_at = datetime(2026, 1, 4, tzinfo=timezone.utc)

    queue_verdict(task)
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.QUEUED
    assert task.verdict_error is None
    assert task.verdict_started_at is None
    assert task.verdict_finished_at is None

    complete_verdict(task, payload=replacement, now=finished_at)
    assert task.verdict is replacement
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict_finished_at == finished_at


@pytest.mark.parametrize(
    "active", [VerdictStatus.PENDING, VerdictStatus.QUEUED, VerdictStatus.RUNNING]
)
def test_cancel_and_failure_do_not_restore_a_superseded_result(active) -> None:
    payload = {"verdict": "accept", "is_good": True}
    task = _task(payload=payload, status=active)
    cancelled_at = datetime.now(timezone.utc)

    cancel_verdict(task, error="quota reached", now=cancelled_at)
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.FAILED
    assert task.verdict_error == "quota reached"
    assert task.verdict_finished_at == cancelled_at

    queue_verdict(task)
    failed_at = datetime.now(timezone.utc)
    fail_verdict(task, error="provider failed", now=failed_at)
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.FAILED
    assert task.verdict_error == "provider failed"
    assert task.verdict_finished_at == failed_at


@pytest.mark.parametrize("previous", ["accept", "reject", None])
def test_completed_qa_without_verdict_clears_the_previous_result(previous) -> None:
    payload = (
        {"verdict": previous, "is_good": previous == "accept"} if previous else None
    )
    task = _task(payload=payload, status=VerdictStatus.RUNNING)
    finished_at = datetime.now(timezone.utc)

    complete_verdict_without_result(task, now=finished_at)

    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict_error is None
    assert task.verdict_finished_at == finished_at
