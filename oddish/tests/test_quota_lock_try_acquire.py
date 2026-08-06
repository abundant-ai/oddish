"""Quota advisory locks: non-blocking enforcement must not queue behind admission."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from oddish.config import QuotaMode, settings
from oddish.core import quota_enforcement, quotas
from oddish.core.quota_enforcement import (
    QuotaLockBusy,
    cancel_trials_if_quota_reached,
    enforce_trial_quotas,
)


@pytest.mark.asyncio
async def test_try_acquire_quota_locks_returns_false_when_org_lock_busy():
    calls: list[str] = []

    async def fake_scalar(_stmt, params=None):
        key = (params or {}).get("key", "")
        calls.append(key)
        return False

    class _Session:
        async def scalar(self, stmt, params=None):
            return await fake_scalar(stmt, params)

    assert await quotas.try_acquire_quota_locks(_Session(), "org-1", "user-1") is False
    assert calls == ["quota:org:org-1"]


@pytest.mark.asyncio
async def test_try_acquire_quota_locks_requires_user_lock_too():
    async def fake_scalar(_stmt, params=None):
        key = (params or {}).get("key", "")
        return key == "quota:org:org-1"

    class _Session:
        async def scalar(self, stmt, params=None):
            return await fake_scalar(stmt, params)

    assert await quotas.try_acquire_quota_locks(_Session(), "org-1", "user-1") is False
    assert await quotas.try_acquire_quota_locks(_Session(), "org-1", None) is True


@pytest.mark.asyncio
async def test_cancel_raises_when_quota_lock_busy(monkeypatch):
    """Lock contention must not look like under-quota (scope=None)."""
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)

    async def busy(*_args, **_kwargs):
        return False

    monkeypatch.setattr(quota_enforcement, "try_acquire_quota_locks", busy)

    with pytest.raises(QuotaLockBusy):
        await cancel_trials_if_quota_reached(
            object(),
            org_id="org-busy",
            billed_user_id="user-busy",
        )


@pytest.mark.asyncio
async def test_enforce_returns_none_on_lock_busy_without_settlement(monkeypatch):
    """Settlement / live-tail must retry: None means check did not complete."""
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def busy(*_args, **_kwargs):
        return False

    after_check_calls = 0

    async def after_check():
        nonlocal after_check_calls
        after_check_calls += 1

    monkeypatch.setattr(quota_enforcement, "get_session", fake_get_session)
    monkeypatch.setattr(quota_enforcement, "try_acquire_quota_locks", busy)

    assert (
        await enforce_trial_quotas(
            org_id="org-busy",
            billed_user_id="user-busy",
            caller_trial_id="trial-1",
            after_check=after_check,
        )
        is None
    )
    assert after_check_calls == 0


def test_append_sweep_preserves_quota_then_task_lock_order():
    """Regression: task FOR UPDATE before admit inverted lock order vs cancel.

    Enforcement takes quota advisory first, then task FOR UPDATE. Append must
    do the same (admit_trials → task FOR UPDATE) or concurrent cancel deadlocks.
    Also: no early acquire_quota_locks across reconcile (starves /tasks/sweep).
    Authoritative reconcile must re-run after the task lock so concurrent
    appends cannot both insert against the same unlocked deficit.
    """
    from oddish.core.endpoints import sweep as sweep_mod

    source = Path(sweep_mod.__file__).read_text(encoding="utf-8")
    assert "await acquire_quota_locks" not in source
    assert "from oddish.core.quotas import acquire_quota_locks" not in source
    assert "async def _plan_append_trials(" in source

    append_start = source.index("if submission.append_to_task:")
    # Limit to the append branch (create mode follows in the same function).
    append_end = source.index("\n    # Create mode", append_start)
    append_body = source[append_start:append_end]
    admit_at = append_body.index("await admit_trials(")
    for_update_at = append_body.index("with_for_update=True")
    assert for_update_at > admit_at, (
        "append path must FOR UPDATE the task only after admit_trials "
        f"(admit@{admit_at}, for_update@{for_update_at})"
    )
    plan_calls = [
        i
        for i in range(len(append_body))
        if append_body.startswith("await _plan_append_trials(", i)
    ]
    assert len(plan_calls) >= 2, (
        "append must plan once for admit sizing and again under the task lock"
    )
    assert plan_calls[0] < admit_at < for_update_at < plan_calls[1], (
        "expected unlocked plan → admit → FOR UPDATE → locked re-plan; "
        f"got plan@{plan_calls} admit@{admit_at} for_update@{for_update_at}"
    )
    # Top-up after a larger locked plan must re-admit the full final count
    # (not a delta): admit_trials adds ``count`` to live inflight only.
    top_up = append_body[plan_calls[1] :]
    assert "count=len(trials)" in top_up
    assert "count=len(trials) - len(planned_trials)" not in top_up
