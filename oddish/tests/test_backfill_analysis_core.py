from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod
from oddish.core import endpoints
from oddish.db import AnalysisStatus, TaskStatus, VerdictStatus


def _trial(trial_id, *, analysis_status=AnalysisStatus.SUCCESS):
    return SimpleNamespace(
        id=trial_id,
        superseded_by_trial_id=None,
        analysis={"classification": "GOOD_SUCCESS"} if analysis_status == AnalysisStatus.SUCCESS else None,
        analysis_status=analysis_status,
        analysis_error=None,
        analysis_started_at=None,
        analysis_finished_at=None,
    )


def _task(trials, *, run_analysis=False, verdict_status=VerdictStatus.SUCCESS):
    return SimpleNamespace(
        id="tsk",
        org_id="org-1",
        trials=trials,
        run_analysis=run_analysis,
        status=TaskStatus.COMPLETED,
        finished_at="ts",
        verdict=None,
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

    async def scalar(self, _stmt):  # _count_active_trials
        return 0

    async def commit(self):
        self.committed = True


@pytest.fixture(autouse=True)
def _stub_enqueue(monkeypatch):
    calls = []

    async def fake_enqueue(session, *, task_id, org_id):
        calls.append((task_id, org_id))

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)
    return calls


@pytest.mark.asyncio
async def test_only_missing_resets_no_trials_but_resets_verdict(_stub_enqueue):
    done = _trial("tsk-0", analysis_status=AnalysisStatus.SUCCESS)
    missing = _trial("tsk-1", analysis_status=None)
    task = _task([done, missing], verdict_status=VerdictStatus.SUCCESS)
    session = _FakeSession(task)

    result = await endpoints.backfill_task_analysis_core(
        session, task_id="tsk", org_id="org-1"
    )

    assert result == {"status": "queued", "task_id": "tsk", "trial_count": 2, "reset_count": 0}
    assert done.analysis_status == AnalysisStatus.SUCCESS  # untouched
    assert task.verdict_status == VerdictStatus.QUEUED       # reset+requeued
    assert task.run_analysis is False                        # flag untouched
    assert _stub_enqueue == [("tsk", "org-1")]


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
    with pytest.raises(endpoints.HTTPException) as exc:
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
    with pytest.raises(endpoints.HTTPException) as exc:
        await endpoints.backfill_task_analysis_core(session, task_id="tsk", org_id="org-1")
    assert exc.value.status_code == 400
