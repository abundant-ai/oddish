"""The worker's retry decision must consult the CURRENT worker_jobs row, not
its claim-time snapshot: an operator capping max_attempts (or a reaper racing)
mid-attempt must bind at the very next failure decision.
"""

from __future__ import annotations

import asyncio

import oddish.workers.queue.worker_job_single_job as wjs
from oddish.workers.jobs.registry import JobFailure, JobOutcome
from oddish.workers.queue.worker_job_single_job import _record_outcome


class _FakeConnection:
    def __init__(self, current_attempts, current_max):
        self._row = {"attempts": current_attempts, "max_attempts": current_max}
        self.executed: list[str] = []

    async def fetchrow(self, sql, *args):
        return dict(self._row)

    async def execute(self, sql, *args):
        self.executed.append(" ".join(sql.split()))
        return "UPDATE 1"

    async def close(self):
        pass


def _run(outcome_kwargs, *, snapshot, current, monkeypatch):
    conn = _FakeConnection(*current)

    async def _fake_open():
        return conn

    monkeypatch.setattr(wjs, "_open_connection", _fake_open)
    ok = asyncio.run(
        _record_outcome(
            job_id="j1",
            worker_id="w1",
            outcome=JobOutcome(
                failure=JobFailure(error_message="boom", retryable=True)
            ),
            attempts=snapshot[0],
            max_attempts=snapshot[1],
        )
    )
    return ok, conn


def test_mid_flight_cap_makes_failure_terminal(monkeypatch):
    # Claim snapshot said 1/6 (retry allowed); the row was capped to 1/1 while
    # the attempt ran. The failure must be TERMINAL, not retried.
    ok, conn = _run({}, snapshot=(1, 6), current=(1, 1), monkeypatch=monkeypatch)
    assert ok is True
    assert any("'FAILED'" in sql or "= 'FAILED'" in sql for sql in conn.executed)
    assert not any("'RETRYING'" in sql for sql in conn.executed)


def test_uncapped_failure_still_retries(monkeypatch):
    ok, conn = _run({}, snapshot=(1, 6), current=(1, 6), monkeypatch=monkeypatch)
    assert ok is True
    assert any("'RETRYING'" in sql for sql in conn.executed)
