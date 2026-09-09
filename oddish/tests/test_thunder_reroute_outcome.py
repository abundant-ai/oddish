"""Provisioned Thunder handoffs retain ownership until sandbox teardown."""

from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import pytest

from oddish.db import WorkerJobKind, WorkerJobStatus
from oddish.workers.queue import worker_job_single_job as runner


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_state", ["RUNNING", "TERMINATING", "TERMINATED"])
async def test_provisioned_handoff_waits_for_teardown(monkeypatch, sandbox_state):
    monkeypatch.setattr(runner.settings, "thunder_capacity_fallback", True)
    monkeypatch.setattr(runner.settings, "thunder_fallback_provider", "modal")
    connection = Mock()
    connection.transaction.return_value = nullcontext()
    connection.fetchrow = AsyncMock(
        side_effect=[
            dict(
                id="job",
                kind="TRIAL",
                status="RUNNING",
                subject_table="trials",
                subject_id="trial",
                attempts=2,
                current_worker_id="worker",
                execution_lane="thunder_trial",
                provider="thunder",
                external_id="sandbox",
            ),
            dict(
                id="trial",
                status="RUNNING",
                environment="thunder",
                attempts=3,
                current_worker_id="worker",
                deleted_at=None,
                superseded_by_trial_id=None,
            ),
            dict(
                id="run",
                state=sandbox_state,
                provider="thunder",
                external_id="sandbox",
                worker_job_attempt=2,
                trial_id="trial",
                deleted_at=None,
            ),
        ]
    )
    connection.fetch = AsyncMock(
        return_value=[
            dict(provider="thunder", slot=0, locked_by="worker", worker_job_id="job")
        ]
    )
    connection.execute = AsyncMock(return_value="UPDATE 1")
    status = await runner._record_reroute_outcome(
        connection,
        job_id="job",
        worker_id="worker",
        attempts=2,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id="trial",
        reroute=runner.JobReroute(
            target_environment="modal",
            target_execution_lane="default",
            reason=runner.THUNDER_CAPACITY_UNAVAILABLE_CODE,
            subject_attempt=3,
        ),
    )
    assert status == WorkerJobStatus.RETRYING
    writes = connection.execute.await_args_list
    assert writes[0].args[2] == "modal"
    assert writes[1].args[2] == "default"
    assert writes[1].args[5] is (sandbox_state != "TERMINATED")
    if sandbox_state == "TERMINATED":
        assert len(writes) == 3
        assert "UPDATE sandbox_capacity_leases" in writes[2].args[0]
    else:
        assert len(writes) == 2
        assert all("UPDATE sandbox_capacity_leases" not in c.args[0] for c in writes)
