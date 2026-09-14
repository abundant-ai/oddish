"""State transitions for the task-level QA verdict.

``tasks.verdict`` is the current QA result. Queuing a replacement withdraws
the previous result; only that replacement can publish a new one. A completed
classification-only pass has SUCCESS status and no verdict. This module is
the single writer for the verdict state columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from oddish.db import VerdictStatus


class VerdictState(Protocol):
    verdict: dict[str, Any] | None
    verdict_status: VerdictStatus | None
    verdict_error: str | None
    verdict_started_at: datetime | None
    verdict_finished_at: datetime | None


ACTIVE_VERDICT_STATUSES = frozenset(
    (VerdictStatus.PENDING, VerdictStatus.QUEUED, VerdictStatus.RUNNING)
)


def has_published_verdict(task: VerdictState) -> bool:
    """Return whether the task has a result from a successful QA pass."""
    return getattr(task, "verdict", None) is not None


def has_active_verdict(task: VerdictState) -> bool:
    """Return whether a replacement QA pass is queued or running."""
    return getattr(task, "verdict_status", None) in ACTIVE_VERDICT_STATUSES


def queue_verdict(task: VerdictState) -> None:
    """Queue a QA pass and withdraw the result it replaces."""
    task.verdict = None
    task.verdict_status = VerdictStatus.QUEUED
    task.verdict_error = None
    task.verdict_started_at = None
    task.verdict_finished_at = None


def complete_verdict(
    task: VerdictState, *, payload: dict[str, Any], now: datetime
) -> None:
    """Publish a replacement verdict after a successful QA pass."""
    task.verdict = payload
    task.verdict_status = VerdictStatus.SUCCESS
    task.verdict_error = None
    task.verdict_finished_at = now


def complete_verdict_without_result(task: VerdictState, *, now: datetime) -> None:
    """Complete a review without presenting the previous verdict as current."""
    task.verdict = None
    task.verdict_status = VerdictStatus.SUCCESS
    task.verdict_error = None
    task.verdict_finished_at = now


def fail_verdict(task: VerdictState, *, error: str, now: datetime) -> None:
    """Fail a QA pass and discard the result that it was replacing."""
    task.verdict = None
    task.verdict_status = VerdictStatus.FAILED
    task.verdict_error = error
    task.verdict_finished_at = now


def cancel_verdict(task: VerdictState, *, error: str, now: datetime) -> None:
    """Cancel QA without restoring a superseded result.

    Cancellation of unrelated trials may also reach this function. Preserve
    a published verdict only when no replacement QA pass was active.
    """
    if has_published_verdict(task) and not has_active_verdict(task):
        _restore_published_verdict(task)
        return
    fail_verdict(task, error=error, now=now)


def abandon_verdict(task: VerdictState) -> None:
    """End superseded or unnecessary QA without recording a failure.

    Appends and retries use this after cancelling an obsolete QA job. An active
    replacement returns to the unstarted state without reviving its old result.
    A published result with no active replacement remains current.
    """
    if has_published_verdict(task) and not has_active_verdict(task):
        _restore_published_verdict(task)
        return
    reset_verdict(task)


def reset_verdict(task: VerdictState) -> None:
    """Remove both the published result and all QA lifecycle metadata."""
    task.verdict = None
    task.verdict_status = None
    task.verdict_error = None
    task.verdict_started_at = None
    task.verdict_finished_at = None


def _restore_published_verdict(task: VerdictState) -> None:
    task.verdict_status = VerdictStatus.SUCCESS
    task.verdict_error = None
    task.verdict_started_at = None
