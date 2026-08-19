"""State transitions for the task-level QA verdict.

``tasks.verdict`` is the last published result. A replacement QA pass may be
queued or running while that result remains visible, so callers must not infer
whether a result exists from ``verdict_status`` alone. This module is the
single writer for the verdict state columns and keeps those two lifecycles
consistent.
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


def queue_verdict(task: VerdictState, *, error: str | None = None) -> None:
    """Queue a QA pass without withdrawing its previously published result."""
    task.verdict_status = VerdictStatus.QUEUED
    task.verdict_error = error
    task.verdict_started_at = None
    if not has_published_verdict(task):
        task.verdict_finished_at = None


def start_verdict(task: VerdictState, *, now: datetime) -> None:
    """Mark the queued QA pass running while retaining any published result."""
    task.verdict_status = VerdictStatus.RUNNING
    task.verdict_error = None
    task.verdict_started_at = now
    if not has_published_verdict(task):
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
    """Finish a no-op QA pass, restoring a prior result when one exists."""
    if has_published_verdict(task):
        _restore_published_verdict(task)
        return
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
    """Cancel QA, restoring a published result or recording terminal failure."""
    if has_published_verdict(task):
        _restore_published_verdict(task)
        return
    fail_verdict(task, error=error, now=now)


def abandon_verdict(task: VerdictState) -> None:
    """End superseded or unnecessary QA without recording a failure.

    Appends and retries use this after cancelling an obsolete QA job. A prior
    result becomes current again; a task that has never produced one returns to
    the unstarted state.
    """
    if has_published_verdict(task):
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
