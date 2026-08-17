"""Importing the worker's provider module installs the auto-enqueue seam.

The oddish side no-ops silently when nothing is registered, so a missing
registration would not fail anything -- trials would just quietly stop getting
summaries. That is what this pins.

It also pins the delegation: the enqueuer must go through
``get_or_enqueue_summary_job`` rather than build its own ``worker_jobs`` row.
That function owns the ``schema_version`` idempotency key, and a second writer
with a different key would not find the first one's job, so a page view after a
trial finished would pay for the same summary twice.
"""

from __future__ import annotations

import pytest

from oddish.workers.queue import trajectory_summary_job as tsj


def test_importing_the_provider_registers_the_enqueuer():
    import worker.trajectory_summary_provider as mod

    assert tsj._enqueuer is mod.enqueue_trajectory_summary


@pytest.mark.asyncio
async def test_the_enqueuer_delegates_to_get_or_enqueue_summary_job(monkeypatch):
    import api.services.summarize_trajectory as svc
    import worker.trajectory_summary_provider as mod

    seen: list[tuple] = []
    sentinel = object()

    async def _fake(session, trial):
        seen.append((session, trial))
        return sentinel

    monkeypatch.setattr(svc, "get_or_enqueue_summary_job", _fake)

    session, trial = object(), object()
    assert await mod.enqueue_trajectory_summary(session, trial) is sentinel
    assert seen == [(session, trial)]
