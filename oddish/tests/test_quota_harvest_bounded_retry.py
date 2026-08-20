"""``_terminate_quota_harvest`` gives up after a bounded number of attempts
and its logs name the targets it retried/abandoned (no database required).

Before the bound, a single unreapable remote target kept the loop -- which
runs in the ``finally`` of ``enforce_trial_quotas`` -- retrying forever,
pinning the worker container and emitting an error record per attempt while
never saying WHICH target was stuck.
"""

from __future__ import annotations

import types

import pytest

import oddish.core.quota_enforcement as qe
from oddish.core.helpers import HarvestTerminationError


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_delay):
        return None

    monkeypatch.setattr(qe, "asyncio", types.SimpleNamespace(sleep=_instant))


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_and_names_targets(monkeypatch, caplog):
    calls = 0

    async def always_incomplete(payload, *, strict):
        nonlocal calls
        calls += 1
        raise HarvestTerminationError(["fc-stuck"], [("daytona", "sb-stuck")])

    monkeypatch.setattr(qe, "terminate_run_harvest", always_incomplete)

    with caplog.at_level("WARNING"):
        await qe._terminate_quota_harvest(
            modal_function_call_ids=["fc-stuck"],
            worker_targets=[("daytona", "sb-stuck")],
            caller_modal_function_call_ids=[],
            org_id="org-1",
            billed_user_id=None,
        )

    assert calls == qe._TERMINATION_MAX_ATTEMPTS
    assert "quota.harvest_abandoned" in caplog.text
    assert "sb-stuck" in caplog.text
    assert "fc-stuck" in caplog.text


@pytest.mark.asyncio
async def test_success_after_narrowing_to_pending_targets(monkeypatch):
    seen_payloads: list[dict] = []

    async def incomplete_once(payload, *, strict):
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            raise HarvestTerminationError(["fc-2"], [])
        return 1

    monkeypatch.setattr(qe, "terminate_run_harvest", incomplete_once)

    await qe._terminate_quota_harvest(
        modal_function_call_ids=["fc-1", "fc-2"],
        worker_targets=[("modal", "w-1")],
        caller_modal_function_call_ids=[],
        org_id=None,
        billed_user_id="user-1",
    )

    # First attempt carries the full harvest; the retry only what's pending.
    assert seen_payloads[0]["modal_function_call_ids"] == ["fc-1", "fc-2"]
    assert seen_payloads[0]["worker_targets"] == [("modal", "w-1")]
    assert seen_payloads[1]["modal_function_call_ids"] == ["fc-2"]
    assert seen_payloads[1]["worker_targets"] == []
    assert len(seen_payloads) == 2


@pytest.mark.asyncio
async def test_unexpected_error_is_also_bounded(monkeypatch, caplog):
    calls = 0

    async def always_boom(payload, *, strict):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider API on fire")

    monkeypatch.setattr(qe, "terminate_run_harvest", always_boom)

    with caplog.at_level("WARNING"):
        await qe._terminate_quota_harvest(
            modal_function_call_ids=["fc-1"],
            worker_targets=[],
            caller_modal_function_call_ids=[],
            org_id="org-1",
            billed_user_id=None,
        )

    assert calls == qe._TERMINATION_MAX_ATTEMPTS
    assert "quota.harvest_abandoned" in caplog.text
