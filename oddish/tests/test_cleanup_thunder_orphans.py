from __future__ import annotations

from typing import Any

import pytest

from oddish.workers.queue import cleanup
from oddish.runtime.backends.thunder import ThunderSandboxSnapshot


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Session:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql = ""

    async def execute(self, statement, _params):
        self.sql = str(statement)
        return _MappingsResult(self.rows)


@pytest.mark.asyncio
async def test_thunder_orphans_are_discovered_from_sandbox_run_ledger():
    session = _Session([{"id": "run-late", "external_id": "tnr-late"}])

    targets = await cleanup._find_orphaned_thunder_sandbox_runs(session)

    assert targets == [("run-late", "tnr-late")]
    assert "run.provider = 'thunder'" in session.sql
    assert "run.state = 'TERMINATING'" in session.sql
    assert "wj.status::text <> 'RUNNING'" in session.sql
    assert "wj.attempts <> run.worker_job_attempt" in session.sql


@pytest.mark.asyncio
async def test_thunder_orphan_termination_uses_lifecycle_ledger(monkeypatch):
    calls: list[str] = []

    async def terminate(run_id: str) -> bool:
        calls.append(run_id)
        return run_id == "run-ok"

    monkeypatch.setattr(
        "oddish.runtime.sandbox_lifecycle.terminate_sandbox_run", terminate
    )

    terminated = await cleanup._terminate_orphaned_sandbox_runs(
        ["run-ok", "run-failed", "run-ok"]
    )

    assert calls == ["run-failed", "run-ok"]
    assert terminated == 1


@pytest.mark.asyncio
async def test_inventory_name_recovers_handle_and_marks_dead_owner_terminating():
    class Result:
        def mappings(self):
            return self

        def one_or_none(self):
            return {
                "id": "run-1",
                "worker_job_id": "job-1",
                "worker_job_attempt": 1,
                "state": "TERMINATING",
            }

    class Session:
        def __init__(self):
            self.params: list[dict[str, Any]] = []

        async def execute(self, _statement, params):
            self.params.append(params)
            return Result()

    session = Session()
    recovered = await cleanup._recover_thunder_inventory_handles(
        session,
        (ThunderSandboxSnapshot(external_id="sb-1", name="run-1"),),
    )

    assert recovered == 1
    assert session.params == [{"sandbox_run_id": "run-1", "external_id": "sb-1"}]


@pytest.mark.asyncio
async def test_absent_unprovisioned_thunder_run_is_finalized_after_inventory_proof():
    class Result:
        rowcount = 1

    class Session:
        def __init__(self):
            self.sql = ""
            self.params: dict[str, Any] = {}

        async def execute(self, statement, params):
            self.sql = str(statement)
            self.params = params
            return Result()

    session = Session()
    finalized = await cleanup._finalize_unprovisioned_thunder_runs(
        session,
        (ThunderSandboxSnapshot(external_id="other", name="other-run"),),
        grace_minutes=30,
    )

    assert finalized == 1
    assert "run.provider = 'thunder'" in session.sql
    assert "run.external_id IS NULL" in session.sql
    assert "run.id::text = ANY" in session.sql
    assert session.params == {"active_names": ["other-run"], "grace_minutes": 30}


@pytest.mark.asyncio
async def test_confirmed_teardown_makes_pending_fallback_dispatchable():
    class UpdateResult:
        rowcount = 1

    class Session:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(self, statement, params):
            sql = str(statement)
            self.calls.append((sql, params))
            if len(self.calls) == 1:
                return _MappingsResult(
                    [
                        {
                            "job_id": "job-1",
                            "trial_id": "trial-1",
                            "target_environment": "modal",
                        }
                    ]
                )
            return UpdateResult()

    session = Session()
    completed = await cleanup._complete_pending_thunder_capacity_handoffs(session)

    assert completed == [("job-1", "trial-1", "modal")]
    select_sql = session.calls[0][0]
    assert "wj.reroute_pending_teardown" in select_sql
    assert "wj.external_id = run.external_id" in select_sql
    assert "run.state = 'TERMINATED'" in select_sql
    job_sql = session.calls[1][0]
    assert "reroute_pending_teardown = false" in job_sql
    assert "available_after = NOW()" in job_sql
    lease_sql = session.calls[2][0]
    assert "UPDATE sandbox_capacity_leases" in lease_sql
    assert "active.terminated_at IS NULL" in lease_sql


@pytest.mark.asyncio
async def test_uncertain_teardown_keeps_pending_fallback_blocked():
    session = _Session([])

    completed = await cleanup._complete_pending_thunder_capacity_handoffs(session)

    assert completed == []
    assert "run.state = 'TERMINATED'" in session.sql
