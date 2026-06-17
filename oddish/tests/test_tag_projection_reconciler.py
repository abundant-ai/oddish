"""Tests for the hourly tag-projection reconciliation in cleanup.py."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    def __init__(self, *, allow_lock=True, last_run_age_minutes=120):
        self.allow_lock = allow_lock
        self.last_run_age_minutes = last_run_age_minutes
        self.statements: list[str] = []

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))

        class _R:
            def all(self_inner):
                return []

        return _R()

    async def scalar(self, stmt, params=None):
        self.statements.append(str(stmt))
        if "pg_try_advisory_xact_lock" in str(stmt):
            return self.allow_lock
        if "last_full_sweep_at" in str(stmt):
            return self.last_run_age_minutes
        return None

    async def flush(self):
        pass


def test_reconciler_no_op_when_recent():
    from oddish.workers.queue import cleanup

    session = _FakeSession(allow_lock=True, last_run_age_minutes=10)
    result = _run(cleanup._maybe_reconcile_tag_projections(session))
    assert result == 0


def test_reconciler_skips_when_lock_busy():
    from oddish.workers.queue import cleanup

    session = _FakeSession(allow_lock=False, last_run_age_minutes=999)
    result = _run(cleanup._maybe_reconcile_tag_projections(session))
    assert result == 0


def test_reconciler_runs_when_due_and_lock_acquired(monkeypatch):
    from oddish.workers.queue import cleanup

    session = _FakeSession(allow_lock=True, last_run_age_minutes=120)

    async def _fake_per_task_recompute(s):
        return 7

    monkeypatch.setattr(
        cleanup,
        "_recompute_drifted_task_projections",
        _fake_per_task_recompute,
    )
    result = _run(cleanup._maybe_reconcile_tag_projections(session))
    assert result == 7
