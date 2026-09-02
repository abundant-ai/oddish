"""The worker's retry decision must consult the CURRENT worker_jobs row, not
its claim-time snapshot: an operator capping max_attempts (or a reaper racing)
mid-attempt must bind at the very next failure decision.
"""

from __future__ import annotations

import asyncio

import oddish.workers.queue.worker_job_single_job as wjs
from oddish.db import WorkerJobStatus
from oddish.workers.jobs.registry import JobFailure, JobOutcome
from oddish.workers.queue.worker_job_single_job import _record_outcome


class _FakeConnection:
    def __init__(self, current_attempts, current_max):
        self._row = {"attempts": current_attempts, "max_attempts": current_max}
        self.statements: list[str] = []

    async def fetchrow(self, sql, *args):
        normalized = " ".join(sql.split())
        self.statements.append(normalized)
        assert normalized.startswith("UPDATE worker_jobs")
        assert "attempts < max_attempts" in normalized
        assert "RETURNING status::text AS status" in normalized
        retryable = bool(args[4])
        status = (
            WorkerJobStatus.RETRYING.value
            if retryable and self._row["attempts"] < self._row["max_attempts"]
            else WorkerJobStatus.FAILED.value
        )
        return {**self._row, "status": status}

    async def execute(self, sql, *args):
        self.statements.append(" ".join(sql.split()))
        return "UPDATE 1"

    async def close(self):
        pass


def _run(outcome_kwargs, *, snapshot, current, monkeypatch):
    conn = _FakeConnection(*current)
    events = []

    async def _fake_open():
        return conn

    monkeypatch.setattr(wjs, "_open_connection", _fake_open)
    monkeypatch.setattr(
        wjs,
        "log_warning",
        lambda message, **attributes: events.append((message, attributes)),
    )
    monkeypatch.setattr(
        wjs,
        "log_error",
        lambda message, **attributes: events.append((message, attributes)),
    )
    status = asyncio.run(
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
    return status, conn, events


def test_mid_flight_cap_makes_failure_terminal(monkeypatch):
    # Claim snapshot said 1/6 (retry allowed); the row was capped to 1/1 while
    # the attempt ran. The failure must be TERMINAL, not retried.
    status, conn, events = _run(
        {}, snapshot=(1, 6), current=(1, 1), monkeypatch=monkeypatch
    )
    assert status == WorkerJobStatus.FAILED
    assert len(conn.statements) == 1
    assert "SELECT attempts, max_attempts" not in conn.statements[0]
    assert events[0][1]["attempt"] == 1
    assert events[0][1]["max_attempts"] == 1


def test_uncapped_failure_still_retries(monkeypatch):
    status, conn, events = _run(
        {}, snapshot=(1, 6), current=(1, 6), monkeypatch=monkeypatch
    )
    assert status == WorkerJobStatus.RETRYING
    assert len(conn.statements) == 1
    assert "SELECT attempts, max_attempts" not in conn.statements[0]
    assert events[0][1]["attempt"] == 1
    assert events[0][1]["max_attempts"] == 6
