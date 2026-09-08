from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from oddish.core import trial_io


def source_trial(*, started: bool = True, has_trajectory: bool = True):
    return SimpleNamespace(
        id="source-1",
        started_at=object() if started else None,
        has_trajectory=has_trajectory,
    )


@pytest.fixture
def evidence_reads(monkeypatch):
    reads = SimpleNamespace(
        result=AsyncMock(return_value={"trial_results": []}),
        verifier=AsyncMock(
            return_value={
                "verifier": {"stdout": "PASS\n", "stderr": None},
                "exception": None,
            }
        ),
        trajectory=AsyncMock(return_value={"steps": []}),
    )
    monkeypatch.setattr(trial_io, "read_trial_result", reads.result)
    monkeypatch.setattr(trial_io, "read_trial_logs_structured", reads.verifier)
    monkeypatch.setattr(trial_io, "read_trial_trajectory", reads.trajectory)
    return reads


@pytest.mark.asyncio
async def test_qa_source_evidence_accepts_readable_started_trial(evidence_reads):
    errors = await trial_io.qa_source_evidence_errors(source_trial())

    assert errors == ()


@pytest.mark.asyncio
async def test_qa_source_evidence_accepts_present_empty_verifier_stream(
    evidence_reads,
):
    evidence_reads.verifier.return_value = {
        "verifier": {"stdout": "", "stderr": None},
        "exception": None,
    }

    errors = await trial_io.qa_source_evidence_errors(source_trial())

    assert errors == ()


@pytest.mark.asyncio
async def test_qa_source_evidence_rejects_stale_trajectory_flag(evidence_reads):
    evidence_reads.verifier.return_value = {
        "verifier": {"stdout": None, "stderr": "failure\n"},
        "exception": None,
    }
    evidence_reads.trajectory.return_value = None

    errors = await trial_io.qa_source_evidence_errors(source_trial())

    assert errors == (
        "has_trajectory=true but no readable trajectory JSON object was found",
    )


@pytest.mark.asyncio
async def test_qa_source_evidence_reports_missing_started_trial_artifacts(
    evidence_reads,
):
    evidence_reads.result.side_effect = HTTPException(
        status_code=404,
        detail="No authoritative result found for source-1",
    )
    evidence_reads.verifier.return_value = {
        "verifier": {"stdout": None, "stderr": None},
        "exception": None,
    }

    errors = await trial_io.qa_source_evidence_errors(source_trial())

    assert errors == (
        "result read failed: HTTPException: No authoritative result found for source-1",
        "verifier stdout, stderr, and exception are unavailable",
    )


@pytest.mark.asyncio
async def test_qa_source_evidence_skips_artifacts_for_unstarted_trial(
    evidence_reads,
):
    errors = await trial_io.qa_source_evidence_errors(
        source_trial(started=False, has_trajectory=False)
    )

    assert errors == ()
    evidence_reads.result.assert_not_awaited()
    evidence_reads.verifier.assert_not_awaited()
    evidence_reads.trajectory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [None, "modal", "daytona"])
async def test_qa_rerun_passes_environment_through_admission(monkeypatch, environment):
    from oddish.core.endpoints.qa import rerun_task_qa_core
    from oddish.db import TaskModel, TrialModel, TrialStatus

    source = TrialModel(
        id="source-1",
        task_id="task-1",
        kind="agent",
        status=TrialStatus.SUCCESS,
    )
    task = TaskModel(id="task-1", name="demo", org_id="org-1", trials=[source])
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: task)
    session.scalar.return_value = 0  # No active solver trials.
    monkeypatch.setattr(
        "oddish.queue.live_analysis_trial_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "oddish.queue.task_audit_pending", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "oddish.queue.qa_eligible_trial_ids", AsyncMock(return_value=[source.id])
    )
    monkeypatch.setattr(
        "oddish.core.endpoints.qa.qa_source_evidence_errors", AsyncMock(return_value=())
    )
    create_qa = AsyncMock()
    monkeypatch.setattr("oddish.workers.analysis_trials.create_qa_trial", create_qa)

    result = await rerun_task_qa_core(
        session,
        task_id=task.id,
        org_id=task.org_id,
        environment=environment,
    )

    assert result["status"] == "queued"
    create_qa.assert_awaited_once_with(
        session,
        task=task,
        eligible_trial_ids=[source.id],
        with_verdict=True,
        environment=environment,
    )
    session.commit.assert_awaited_once()
