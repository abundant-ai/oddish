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
    start_verdict,
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


@pytest.mark.parametrize("active", [VerdictStatus.QUEUED, VerdictStatus.RUNNING])
def test_abandon_active_replacement_restores_published_verdict(
    active: VerdictStatus,
) -> None:
    payload = {"verdict": "accept", "is_good": True}
    task = _task(payload=payload, status=active)
    published_at = task.verdict_finished_at

    abandon_verdict(task)

    assert task.verdict is payload
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict_error is None
    assert task.verdict_started_at is None
    assert task.verdict_finished_at == published_at


def test_abandon_first_qa_attempt_returns_to_unstarted_state() -> None:
    task = _task(status=VerdictStatus.RUNNING)

    abandon_verdict(task)

    assert task.verdict is None
    assert task.verdict_status is None
    assert task.verdict_error is None
    assert task.verdict_started_at is None
    assert task.verdict_finished_at is None


def test_replacement_lifecycle_retains_published_result_until_success() -> None:
    original = {"verdict": "reject", "is_good": False}
    replacement = {"verdict": "accept", "is_good": True}
    task = _task(payload=original, status=VerdictStatus.SUCCESS)
    published_at = task.verdict_finished_at
    started_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
    finished_at = datetime(2026, 1, 4, tzinfo=timezone.utc)

    queue_verdict(task)
    assert task.verdict is original
    assert task.verdict_status == VerdictStatus.QUEUED
    assert task.verdict_finished_at == published_at

    start_verdict(task, now=started_at)
    assert task.verdict is original
    assert task.verdict_status == VerdictStatus.RUNNING
    assert task.verdict_started_at == started_at
    assert task.verdict_finished_at == published_at

    complete_verdict(task, payload=replacement, now=finished_at)
    assert task.verdict is replacement
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict_finished_at == finished_at


def test_cancel_replacement_restores_result_but_failure_discards_it() -> None:
    payload = {"verdict": "accept", "is_good": True}
    task = _task(payload=payload, status=VerdictStatus.RUNNING)
    published_at = task.verdict_finished_at

    cancel_verdict(task, error="quota reached", now=datetime.now(timezone.utc))
    assert task.verdict is payload
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict_finished_at == published_at

    queue_verdict(task)
    failed_at = datetime.now(timezone.utc)
    fail_verdict(task, error="provider failed", now=failed_at)
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.FAILED
    assert task.verdict_error == "provider failed"
    assert task.verdict_finished_at == failed_at


def test_noop_qa_restores_existing_result() -> None:
    payload = {"verdict": "accept", "is_good": True}
    task = _task(payload=payload, status=VerdictStatus.RUNNING)
    published_at = task.verdict_finished_at

    complete_verdict_without_result(task, now=datetime.now(timezone.utc))

    assert task.verdict is payload
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict_started_at is None
    assert task.verdict_finished_at == published_at
