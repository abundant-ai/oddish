"""Release-path guarantees for the pre-trial claim in `_run_pre_trial_audit`:
whatever ends the audit — success, synth failure, a double-fault while
recording that failure, or job cancellation — a claim that persisted nothing
must be released rather than left RUNNING until the lease expires. All DB
touchpoints are monkeypatched; no real session is opened."""

from __future__ import annotations

import asyncio

import pytest

import oddish.workers.queue.qa_handler as qa


class _Recorder:
    def __init__(self):
        self.released: list[str] = []

    async def release(self, task_version_id: str) -> None:
        self.released.append(task_version_id)


@pytest.fixture
def wired(monkeypatch):
    """Wire a claimed version + enabled pre-trial; return the release recorder."""
    rec = _Recorder()

    async def fake_claim(task_id):
        return "tv1"

    monkeypatch.setattr(qa, "_claim_pre_trial_version", fake_claim)
    monkeypatch.setattr(qa, "_release_pre_trial_claim", rec.release)
    monkeypatch.setattr(qa.settings, "pre_trial_enabled", True)
    monkeypatch.setattr(qa.settings, "pre_trial_timeout", 5.0)
    return rec


@pytest.mark.asyncio
async def test_cancelled_synth_releases_claim(wired, monkeypatch):
    async def cancelled_synth(task_id, version_id, trial_ids, timeout):
        raise asyncio.CancelledError()

    monkeypatch.setattr(qa, "_pre_trial_synth_fn", cancelled_synth)

    with pytest.raises(asyncio.CancelledError):
        await qa._run_pre_trial_audit("task1", "job1", ["t1"])
    # CancelledError must still propagate (the job IS being cancelled),
    # but not before the claim is rolled back.
    assert wired.released == ["tv1"]


@pytest.mark.asyncio
async def test_failure_sync_double_fault_still_releases(wired, monkeypatch):
    async def failing_synth(task_id, version_id, trial_ids, timeout):
        raise RuntimeError("sandbox died")

    async def failing_sync(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(qa, "_pre_trial_synth_fn", failing_synth)
    monkeypatch.setattr(qa, "sync_pre_trial_to_task_version", failing_sync)

    # Swallowed (pre-trial must never block the verdict path) …
    await qa._run_pre_trial_audit("task1", "job1", ["t1"])
    # … but the claim is released even though recording the failure failed.
    assert wired.released == ["tv1"]


@pytest.mark.asyncio
async def test_stored_result_keeps_claim(wired, monkeypatch):
    async def ok_synth(task_id, version_id, trial_ids, timeout):
        return []

    async def ok_sync(*args, **kwargs):
        return "tv1"

    monkeypatch.setattr(qa, "_pre_trial_synth_fn", ok_synth)
    monkeypatch.setattr(qa, "sync_pre_trial_to_task_version", ok_sync)

    await qa._run_pre_trial_audit("task1", "job1", ["t1"])
    # A persisted terminal status must NOT be rolled back to unclaimed.
    assert wired.released == []


@pytest.mark.asyncio
async def test_vetoed_store_releases(wired, monkeypatch):
    async def ok_synth(task_id, version_id, trial_ids, timeout):
        return []

    async def vetoed_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(qa, "_pre_trial_synth_fn", ok_synth)
    monkeypatch.setattr(qa, "sync_pre_trial_to_task_version", vetoed_sync)

    await qa._run_pre_trial_audit("task1", "job1", ["t1"])
    assert wired.released == ["tv1"]


@pytest.mark.asyncio
async def test_no_claim_no_release(wired, monkeypatch):
    async def no_claim(task_id):
        return None

    monkeypatch.setattr(qa, "_claim_pre_trial_version", no_claim)

    async def exploding_synth(*a):  # pragma: no cover - must not be reached
        raise AssertionError("synth must not run without a claim")

    monkeypatch.setattr(qa, "_pre_trial_synth_fn", exploding_synth)

    await qa._run_pre_trial_audit("task1", "job1", ["t1"])
    assert wired.released == []
