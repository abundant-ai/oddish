"""Tests for ``drain_worker_jobs`` -- the batch-drain loop that keeps a worker
container's slot busy across many short jobs instead of one-and-idle.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue.worker_job_single_job import drain_worker_jobs  # noqa: E402


@pytest.mark.asyncio
async def test_drain_stops_when_queue_empties():
    """Keeps claiming until ``run_job`` reports the queue empty."""
    calls = {"n": 0}

    async def run_job(queue_key, **kwargs):
        calls["n"] += 1
        return calls["n"] <= 3  # three jobs, then empty

    processed = await drain_worker_jobs(
        "q",
        worker_id="w",
        queue_slot=0,
        budget_seconds=10_000,
        _run_job=run_job,
        _now=lambda: 0.0,
    )

    assert processed == 3
    assert calls["n"] == 4  # the 4th claim found an empty queue


@pytest.mark.asyncio
async def test_drain_stops_at_wall_clock_budget():
    """A never-empty queue of short jobs stops once the budget is spent."""
    clock = {"t": 0.0}

    async def run_job(queue_key, **kwargs):
        clock["t"] += 60.0  # each job burns 60s
        return True  # queue never drains

    processed = await drain_worker_jobs(
        "q",
        worker_id="w",
        queue_slot=0,
        budget_seconds=300.0,
        _run_job=run_job,
        _now=lambda: clock["t"],
    )

    # deadline = 0 + 300; jobs land the clock at 60,120,180,240,300 -> stop at 5.
    assert processed == 5


@pytest.mark.asyncio
async def test_long_job_runs_once_per_container():
    """A single job longer than the budget still runs exactly one-per-container
    (so agent trials are unaffected by batching)."""
    ticks = iter([0.0, 999.0])  # start, then post-job clock is past the deadline

    async def run_job(queue_key, **kwargs):
        return True

    processed = await drain_worker_jobs(
        "q",
        worker_id="w",
        queue_slot=0,
        budget_seconds=300.0,
        _run_job=run_job,
        _now=lambda: next(ticks),
    )

    assert processed == 1


@pytest.mark.asyncio
async def test_drain_empty_queue_returns_zero():
    async def run_job(queue_key, **kwargs):
        return False

    processed = await drain_worker_jobs(
        "q",
        worker_id="w",
        queue_slot=0,
        budget_seconds=300.0,
        _run_job=run_job,
        _now=lambda: 0.0,
    )

    assert processed == 0


@pytest.mark.asyncio
async def test_drain_passes_slot_and_hooks_through():
    """Each claim reuses the same held slot and forwards the call args."""
    seen = []

    async def run_job(
        queue_key,
        *,
        worker_id,
        queue_slot,
        modal_function_call_id,
        post_success_hooks,
        harbor_variant_id="default",
    ):
        seen.append((queue_key, worker_id, queue_slot, modal_function_call_id))
        return len(seen) < 2

    hooks = {}
    await drain_worker_jobs(
        "analysis-key",
        worker_id="worker-1",
        queue_slot=7,
        budget_seconds=1000.0,
        modal_function_call_id="fc-123",
        post_success_hooks=hooks,
        _run_job=run_job,
        _now=lambda: 0.0,
    )

    assert seen == [
        ("analysis-key", "worker-1", 7, "fc-123"),
        ("analysis-key", "worker-1", 7, "fc-123"),
    ]
