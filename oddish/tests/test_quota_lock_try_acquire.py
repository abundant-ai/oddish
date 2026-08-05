"""Quota advisory locks: non-blocking enforcement must not queue behind admission."""

from __future__ import annotations

import pytest

from oddish.config import QuotaMode, settings
from oddish.core import quota_enforcement, quotas
from oddish.core.quota_enforcement import cancel_trials_if_quota_reached


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
async def test_enforcement_skips_when_quota_lock_busy(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)

    async def busy(*_args, **_kwargs):
        return False

    monkeypatch.setattr(quota_enforcement, "try_acquire_quota_locks", busy)

    result = await cancel_trials_if_quota_reached(
        object(),
        org_id="org-busy",
        billed_user_id="user-busy",
    )
    assert result["scope"] is None
    assert result["trials_cancelled"] == 0


def test_append_sweep_does_not_hold_quota_locks_across_reconcile():
    """Regression: early org lock on append starved concurrent /tasks/sweep."""
    from pathlib import Path

    from oddish.core.endpoints import sweep as sweep_mod

    source = Path(sweep_mod.__file__).read_text(encoding="utf-8")
    assert "await acquire_quota_locks" not in source
    assert "from oddish.core.quotas import acquire_quota_locks" not in source
