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
