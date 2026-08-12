from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import HTTPException

import oddish.queue as queue_mod
from oddish.core import endpoints
from oddish.db import AnalysisStatus, TaskStatus, VerdictStatus, utcnow


def _trial(
    trial_id,
    *,
    analysis_status=AnalysisStatus.SUCCESS,
    task_version_id=None,
):
    return SimpleNamespace(
        id=trial_id,
        superseded_by_trial_id=None,
        task_version_id=task_version_id,
        analysis=(
            {"classification": "GOOD_SUCCESS"}
            if analysis_status == AnalysisStatus.SUCCESS
            else None
        ),
        analysis_status=analysis_status,
        analysis_error=None,
        analysis_started_at=None,
        analysis_finished_at=None,
    )


def _task(
    trials,
    *,
    run_analysis=False,
    verdict=None,
    verdict_status=VerdictStatus.SUCCESS,
    current_version_id=None,
):
    return SimpleNamespace(
        id="tsk",
        org_id="org-1",
        trials=trials,
        current_version_id=current_version_id,
        run_analysis=run_analysis,
        status=TaskStatus.COMPLETED,
        finished_at="ts",
        verdict=verdict,
        verdict_status=verdict_status,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
    )


class _Result:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    def __init__(self, task):
        self.task = task
        self.committed = False

    async def execute(self, _stmt):
        return _Result(self.task)

    async def scalar(self, stmt):
        # _count_active_trials wants an int; the worker-job guard queries
        # want "no such job". Discriminate on the aggregate.
        return 0 if "count(" in str(stmt).lower() else None

    async def commit(self):
        self.committed = True


@pytest.fixture(autouse=True)
def _stub_enqueue(monkeypatch):
    calls = []

    async def fake_enqueue(
        session,
        *,
        task_id,
        task_version_id,
        task_version_content_hash,
        org_id,
    ):
        calls.append((task_id, task_version_id, task_version_content_hash, org_id))

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)
    return calls


@pytest.mark.asyncio
async def test_only_missing_preserves_verdict_while_replacement_is_queued(
    _stub_enqueue,
):
    done = _trial("tsk-0", analysis_status=AnalysisStatus.SUCCESS)
    missing = _trial("tsk-1", analysis_status=None)
    payload = {"verdict": "accept", "is_good": True}
    task = _task([done, missing], verdict=payload, verdict_status=VerdictStatus.SUCCESS)
    session = _FakeSession(task)

    result = await endpoints.backfill_task_analysis_core(
        session, task_id="tsk", org_id="org-1"
    )

    assert result == {
        "status": "queued",
        "task_id": "tsk",
        "trial_count": 2,
        "reset_count": 0,
    }
    assert done.analysis_status == AnalysisStatus.SUCCESS  # untouched
    assert task.verdict is payload
    assert task.verdict_status == VerdictStatus.QUEUED
    assert task.run_analysis is False  # flag untouched
    assert _stub_enqueue == [("tsk", None, None, "org-1")]


@pytest.mark.asyncio
async def test_force_resets_all_live_trials(_stub_enqueue):
    a = _trial("tsk-0", analysis_status=AnalysisStatus.SUCCESS)
    b = _trial("tsk-1", analysis_status=AnalysisStatus.FAILED)
    task = _task([a, b])
    session = _FakeSession(task)

    result = await endpoints.backfill_task_analysis_core(
        session, task_id="tsk", org_id="org-1", force=True
    )

    assert result["reset_count"] == 2
    assert a.analysis_status is None and b.analysis_status is None


@pytest.mark.asyncio
async def test_force_resets_only_current_version_trials(_stub_enqueue):
    historical = _trial("tsk-old", task_version_id="tsk-v1")
    current = _trial("tsk-current", task_version_id="tsk-v2")
    task = _task(
        [historical, current],
        current_version_id="tsk-v2",
    )
    session = _FakeSession(task)

    result = await endpoints.backfill_task_analysis_core(
        session, task_id="tsk", org_id="org-1", force=True
    )

    assert result["trial_count"] == 1
    assert result["reset_count"] == 1
    assert historical.analysis_status == AnalysisStatus.SUCCESS
    assert current.analysis_status is None
    assert _stub_enqueue == [("tsk", "tsk-v2", None, "org-1")]


@pytest.mark.asyncio
async def test_force_with_trial_ids_resets_only_named(_stub_enqueue):
    a = _trial("tsk-0", analysis_status=AnalysisStatus.SUCCESS)
    b = _trial("tsk-1", analysis_status=AnalysisStatus.SUCCESS)
    task = _task([a, b])
    session = _FakeSession(task)

    result = await endpoints.backfill_task_analysis_core(
        session, task_id="tsk", org_id="org-1", force=True, trial_ids=["tsk-1"]
    )

    assert result["reset_count"] == 1
    assert a.analysis_status == AnalysisStatus.SUCCESS
    assert b.analysis_status is None


@pytest.mark.asyncio
async def test_enable_analysis_flips_flag(_stub_enqueue):
    task = _task([_trial("tsk-0")], run_analysis=False)
    session = _FakeSession(task)

    await endpoints.backfill_task_analysis_core(
        session, task_id="tsk", org_id="org-1", enable_analysis=True
    )
    assert task.run_analysis is True


@pytest.mark.asyncio
async def test_org_mismatch_404(_stub_enqueue):
    task = _task([_trial("tsk-0")])
    session = _FakeSession(task)
    with pytest.raises(HTTPException) as exc:
        await endpoints.backfill_task_analysis_core(
            session, task_id="tsk", org_id="other-org"
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_qa_in_progress_400(_stub_enqueue):
    task = _task(
        [_trial("tsk-0", analysis_status=AnalysisStatus.RUNNING)],
        verdict_status=VerdictStatus.RUNNING,
    )
    session = _FakeSession(task)
    with pytest.raises(HTTPException) as exc:
        await endpoints.backfill_task_analysis_core(
            session, task_id="tsk", org_id="org-1"
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_live_running_analysis_blocks(_stub_enqueue):
    running = _trial("tsk-0", analysis_status=AnalysisStatus.RUNNING)
    running.analysis_started_at = utcnow()
    task = _task([running], verdict_status=VerdictStatus.SUCCESS)
    session = _FakeSession(task)
    with pytest.raises(HTTPException) as exc:
        await endpoints.backfill_task_analysis_core(
            session, task_id="tsk", org_id="org-1"
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_stale_running_analysis_does_not_block(_stub_enqueue):
    """A RUNNING claim past its TTL belongs to a dead worker. The backfill
    is the recovery path, so the stale claim must not block it — the same
    rule the per-trial rerun applies."""
    stale = _trial("tsk-0", analysis_status=AnalysisStatus.RUNNING)
    stale.analysis_started_at = utcnow() - timedelta(hours=2)
    task = _task([stale], verdict_status=VerdictStatus.SUCCESS)
    session = _FakeSession(task)

    result = await endpoints.backfill_task_analysis_core(
        session, task_id="tsk", org_id="org-1"
    )

    assert result["status"] == "queued"
    assert _stub_enqueue == [("tsk", None, None, "org-1")]
