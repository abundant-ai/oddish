"""Batch sweep payer-lock ordering.

Two batch transactions acquiring per-payer advisory locks in item order deadlock
ABBA when their payer orders differ. The batch core must pre-resolve every
item's payer, take the DISTINCT locks in SORTED order before any item runs, and
reuse the pre-resolved ids in the loop (no per-item re-resolution).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.core.endpoints.sweep as sweep_mod  # noqa: E402
import oddish.core.quota_admission as quota_admission  # noqa: E402
import oddish.core.sweeps as sweeps_mod  # noqa: E402
from oddish.core.endpoints.sweep import create_task_sweep_batch_core  # noqa: E402


class _FakeSavepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def begin_nested(self):
        return _FakeSavepoint()


@pytest.mark.asyncio
async def test_batch_acquires_distinct_payer_locks_sorted_before_items(monkeypatch):
    events: list[tuple] = []

    async def fake_acquire(session, org_id, payer):
        events.append(("lock", org_id, payer))

    async def fake_create(session, *, submission, org_id, billed_user_id, **kwargs):
        events.append(("item", billed_user_id))
        raise HTTPException(status_code=404, detail="stop after recording")

    monkeypatch.setattr(quota_admission, "acquire_payer_lock", fake_acquire)
    monkeypatch.setattr(sweep_mod, "create_task_sweep_core", fake_create)
    monkeypatch.setattr(sweeps_mod, "validate_sweep_submission", lambda _s: None)

    submissions = [SimpleNamespace(), SimpleNamespace(), SimpleNamespace()]
    payers = {id(submissions[0]): "zed", id(submissions[1]): "amy", id(submissions[2]): "zed"}
    resolve_calls: list[object] = []

    async def prepare(session, submission):
        # Identity resolution must precede payer resolution per item (the
        # single route's identity -> attribution -> owner order).
        events.append(("prepare", id(submission)))
        return None

    async def resolve(session, submission):
        assert ("prepare", id(submission)) in events
        resolve_calls.append(submission)
        return payers[id(submission)]

    results = await create_task_sweep_batch_core(
        _FakeSession(),
        submissions=submissions,
        org_id="org-1",
        prepare=prepare,
        resolve_billed_user_id=resolve,
    )

    lock_events = [e for e in events if e[0] == "lock"]
    item_events = [e for e in events if e[0] == "item"]
    # Distinct payers locked in sorted order, all before any item runs.
    assert lock_events == [("lock", "org-1", "amy"), ("lock", "org-1", "zed")]
    assert events.index(lock_events[-1]) < events.index(item_events[0])
    # Pre-resolved ids are reused in item order -- resolution ran exactly once
    # per item, before the loop.
    assert item_events == [("item", "zed"), ("item", "amy"), ("item", "zed")]
    assert len(resolve_calls) == 3
    assert [r.success for r in results] == [False, False, False]


@pytest.mark.asyncio
async def test_pre_loop_resolution_failure_fails_only_that_item(monkeypatch):
    events: list[tuple] = []

    async def fake_acquire(session, org_id, payer):
        events.append(("lock", payer))

    async def fake_create(session, *, submission, org_id, billed_user_id, **kwargs):
        events.append(("item", billed_user_id))
        raise HTTPException(status_code=404, detail="stop after recording")

    monkeypatch.setattr(quota_admission, "acquire_payer_lock", fake_acquire)
    monkeypatch.setattr(sweep_mod, "create_task_sweep_core", fake_create)
    monkeypatch.setattr(sweeps_mod, "validate_sweep_submission", lambda _s: None)

    good, bad = SimpleNamespace(), SimpleNamespace()

    async def resolve(session, submission):
        if submission is bad:
            raise HTTPException(status_code=403, detail={"message": "unlinked"})
        return "amy"

    results = await create_task_sweep_batch_core(
        _FakeSession(),
        submissions=[bad, good],
        org_id="org-1",
        resolve_billed_user_id=resolve,
    )

    # The failed item never locks and never runs; the good one proceeds.
    assert events == [("lock", "amy"), ("item", "amy")]
    assert results[0].success is False
    assert results[0].status_code == 403
    assert results[0].error == "unlinked"
    assert results[1].status_code == 404  # reached creation
