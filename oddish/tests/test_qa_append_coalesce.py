"""Appends must coalesce into one QA pass instead of one pass each.

A task that is appended to repeatedly (sweep auto-detects an existing task and
sets ``append_to_task``) re-arms QA every round: the append wipes the verdict
and cancels the in-flight QA job, then ``maybe_start_qa_stage`` enqueues a
fresh one once the new trials settle. Per-trial classification is deduped, but
the verdict synthesis re-runs over the *whole* live trial set every time --
O(trials) work per 5-trial append, holding a QA queue slot each round.

Leaving the already-queued QA row alone is not an option: a QUEUED row means
"every trial was settled when I was enqueued", and appending breaks that
invariant (the row would classify trials that have not run yet). So the QA row
is instead enqueued with an ``available_after`` debounce -- appends inside the
window cancel and re-arm a row that was never claimable, and exactly one pass
runs once appends go quiet.

Fake session: ``enqueue_worker_job`` needs only add/flush and
``worker_jobs.subject_id`` is not a foreign key, so no live DB is required.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
from oddish.config import settings  # noqa: E402
from oddish.db import TaskStatus, VerdictStatus, utcnow  # noqa: E402
from oddish.queue import enqueue_qa_worker_job  # noqa: E402


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_qa_job_is_debounced_by_the_coalesce_window():
    """A coalesce window makes the QA row unclaimable until it elapses."""
    session = _FakeSession()

    row = await enqueue_qa_worker_job(
        session, task_id="task1", org_id="org123", delay_seconds=120
    )

    # The claim enforces ``available_after <= NOW()``, so a future stamp is
    # what actually withholds the row from a worker.
    assert row.available_after > utcnow() + timedelta(seconds=100)


@pytest.mark.asyncio
async def test_qa_job_is_immediately_claimable_without_a_coalesce_window():
    """Zero delay must preserve the pre-debounce behaviour exactly."""
    session = _FakeSession()

    row = await enqueue_qa_worker_job(
        session, task_id="task1", org_id="org123", delay_seconds=0
    )

    assert row.available_after <= utcnow()


# ---------------------------------------------------------------------------
# Stage transition wiring
# ---------------------------------------------------------------------------


class _ForUpdateResult:
    def __init__(self, task):
        self._task = task

    def scalar_one_or_none(self):
        return self._task


class _StageSession:
    """Minimal session for ``maybe_start_qa_stage`` (see test_task_qa_pipeline)."""

    def __init__(self, *, trial, task, pending_count, qa_eligible=1):
        self._trial = trial
        self._task = task
        self._counts = [pending_count, qa_eligible]
        self._scalar_calls = 0

    async def get(self, _model, _key):
        return self._trial

    async def execute(self, _statement):
        return _ForUpdateResult(self._task)

    async def scalar(self, _statement):
        index = self._scalar_calls
        self._scalar_calls += 1
        return self._counts[index] if index < len(self._counts) else 0

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_stage_enqueues_qa_with_the_configured_coalesce_window(monkeypatch):
    """The settle path must apply the debounce, not enqueue at NOW()."""
    monkeypatch.setattr(settings, "qa_coalesce_seconds", 90, raising=False)

    trial = SimpleNamespace(task_id="task-1")
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, pending_count=0)

    delays: list[int] = []

    async def fake_enqueue(_session, *, task_id, org_id, delay_seconds=0):
        delays.append(delay_seconds)

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)

    started = await queue_mod.maybe_start_qa_stage(session, "task-1-0")

    assert started is True
    assert delays == [90]
    # The task still advances immediately -- only the worker pickup is delayed,
    # so the dashboard shows VERDICT_PENDING rather than a stalled RUNNING.
    assert task.status == TaskStatus.VERDICT_PENDING
    assert task.verdict_status == VerdictStatus.QUEUED
