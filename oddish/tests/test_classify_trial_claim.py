"""A trial already being classified must not be classified again.

Concurrent QA jobs for one task are normal (a sweep append enqueues one per
batch, and a stale-reaped job is retried alongside the run it duplicated), so
the claim is the only thing standing between that concurrency and paying for
the same classification N times.
"""

from __future__ import annotations

import asyncio
import types
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from oddish.analyze import TrialClassifier
from oddish.analyze.analysis_cost import AnalysisUsage
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    utcnow,
)
from oddish.db.models import AnalysisCostModel


async def _seed(session, trial_id: str, *, experiment_id: str) -> str:
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id="org-x"))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id="org-x",
            user="tester",
            task_path="s3://test-bucket/claim-fake-task",
            status=TaskStatus.COMPLETED,
        )
    )
    session.add(
        TrialModel(
            id=trial_id,
            name=trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id="org-x",
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            model="gpt-5",
            is_probe=False,
            max_attempts=6,
            billed_user_id="user-x",
        )
    )
    await session.flush()
    await session.commit()
    return task_id


async def _cleanup(session, *, task_id: str, experiment_id: str, trial_id: str) -> None:
    await session.execute(
        AnalysisCostModel.__table__.delete().where(
            AnalysisCostModel.trial_id == trial_id
        )
    )
    await session.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))
    await session.execute(
        ExperimentModel.__table__.delete().where(ExperimentModel.id == experiment_id)
    )
    await session.commit()


def _fake_classification():
    return types.SimpleNamespace(
        trial_name="t",
        classification=types.SimpleNamespace(value="GOOD"),
        subtype="none",
        evidence="looks fine",
        root_cause=None,
        recommendation=None,
        reward=1.0,
        action_items=[],
        exploitation=[],
    )


def _usage() -> AnalysisUsage:
    return AnalysisUsage(
        cost_usd=0.05,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=0,
        cache_write_tokens=0,
        model="anthropic/claude-sonnet-5",
        source="native",
    )


def _stub_directories(monkeypatch, ah) -> None:
    async def _fake_dir(**kwargs):
        return Path("/tmp"), None, "key"

    monkeypatch.setattr(ah, "resolve_task_directory", _fake_dir)
    monkeypatch.setattr(ah, "resolve_trial_directory", _fake_dir)


@pytest.mark.asyncio
async def test_second_caller_skips_a_trial_already_being_classified(
    session, monkeypatch
):
    from oddish.workers.queue import analysis_handler as ah

    trial_id = f"trial-claim-{uuid.uuid4().hex[:8]}"
    experiment_id = f"exp-{uuid.uuid4().hex[:8]}"
    task_id = await _seed(session, trial_id, experiment_id=experiment_id)

    calls = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_classify(self, **kwargs):
        nonlocal calls
        calls += 1
        # Only the first caller parks, so a regression fails the assertion
        # instead of deadlocking the suite.
        if calls == 1:
            first_started.set()
            await release.wait()
        self.last_usage = _usage()
        return _fake_classification()

    monkeypatch.setattr(TrialClassifier, "classify_trial", _fake_classify)
    _stub_directories(monkeypatch, ah)

    try:
        first = asyncio.create_task(ah.classify_trial_and_store(trial_id))
        # The claim is committed before classify_trial runs, so this event
        # means the RUNNING claim is durably visible to the second caller.
        await asyncio.wait_for(first_started.wait(), timeout=10)

        skipped_status = await ah.classify_trial_and_store(trial_id)

        release.set()
        await asyncio.wait_for(first, timeout=10)

        assert skipped_status == AnalysisStatus.RUNNING
        # The classify call count IS the duplicate-spend check: since #898 the
        # ledger row is written by AnalyzerBlock, not by this handler, so
        # counting analysis_costs rows here would prove nothing either way.
        assert calls == 1, "a trial under active classification was re-classified"
    finally:
        release.set()
        await _cleanup(
            session, task_id=task_id, experiment_id=experiment_id, trial_id=trial_id
        )


@pytest.mark.asyncio
async def test_stale_running_claim_is_retaken(session, monkeypatch):
    """A worker that died mid-classification must not strand the trial."""
    from oddish.workers.queue import analysis_handler as ah

    trial_id = f"trial-stale-{uuid.uuid4().hex[:8]}"
    experiment_id = f"exp-{uuid.uuid4().hex[:8]}"
    task_id = await _seed(session, trial_id, experiment_id=experiment_id)

    trial = await session.get(TrialModel, trial_id)
    trial.analysis_status = AnalysisStatus.RUNNING
    trial.analysis_started_at = utcnow() - timedelta(
        minutes=ah.ANALYSIS_CLAIM_TTL_MINUTES + 5
    )
    await session.commit()

    calls = 0

    async def _fake_classify(self, **kwargs):
        nonlocal calls
        calls += 1
        self.last_usage = _usage()
        return _fake_classification()

    monkeypatch.setattr(TrialClassifier, "classify_trial", _fake_classify)
    _stub_directories(monkeypatch, ah)

    try:
        await ah.classify_trial_and_store(trial_id)
        assert calls == 1, "an abandoned RUNNING claim was never retaken"
    finally:
        await _cleanup(
            session, task_id=task_id, experiment_id=experiment_id, trial_id=trial_id
        )


@pytest.mark.asyncio
async def test_orphaned_owner_still_stores_so_the_trial_is_not_reclassified(
    session, monkeypatch
):
    """A classification outliving its QA job must still land a terminal status.

    The tokens are spent the moment the classifier returns. If the owning
    worker job died in the meantime and the result is dropped without stamping
    ``analysis_status``, the trial keeps its own RUNNING claim, ages past
    ANALYSIS_CLAIM_TTL_MINUTES, and the next QA sweep classifies it again --
    paying a second time for a classification that already succeeded.
    """
    from oddish.workers.queue import analysis_handler as ah

    trial_id = f"trial-orphan-{uuid.uuid4().hex[:8]}"
    experiment_id = f"exp-{uuid.uuid4().hex[:8]}"
    task_id = await _seed(session, trial_id, experiment_id=experiment_id)

    calls = 0

    async def _fake_classify(self, **kwargs):
        nonlocal calls
        calls += 1
        self.last_usage = _usage()
        return _fake_classification()

    monkeypatch.setattr(TrialClassifier, "classify_trial", _fake_classify)
    _stub_directories(monkeypatch, ah)

    async def _owner_is_gone(_session) -> bool:
        return False

    try:
        status = await ah.classify_trial_and_store(
            trial_id, should_store=_owner_is_gone
        )
        assert calls == 1
        assert status == AnalysisStatus.SUCCESS

        await session.rollback()
        trial = await session.get(TrialModel, trial_id)
        await session.refresh(trial)
        assert trial.analysis_status == AnalysisStatus.SUCCESS, (
            "a paid classification was dropped without a terminal status"
        )
        assert trial.analysis is not None

        # Age the claim past the TTL, exactly as a later sweep would find it,
        # and confirm the trial is no longer eligible for re-classification.
        trial.analysis_started_at = utcnow() - timedelta(
            minutes=ah.ANALYSIS_CLAIM_TTL_MINUTES + 5
        )
        await session.commit()

        await ah.classify_trial_and_store(trial_id)
        assert calls == 1, "an already-classified trial was re-classified and re-billed"
    finally:
        await _cleanup(
            session, task_id=task_id, experiment_id=experiment_id, trial_id=trial_id
        )


@pytest.mark.asyncio
async def test_result_is_dropped_when_another_worker_retook_the_claim(
    session, monkeypatch
):
    """A slow classification must not clobber the newer owner's analysis.

    Storing unconditionally would fix the re-billing but reintroduce a worse
    bug: a worker whose claim expired mid-run finishing last and overwriting
    the result of the worker that legitimately retook the trial.
    """
    from oddish.workers.queue import analysis_handler as ah

    trial_id = f"trial-retaken-{uuid.uuid4().hex[:8]}"
    experiment_id = f"exp-{uuid.uuid4().hex[:8]}"
    task_id = await _seed(session, trial_id, experiment_id=experiment_id)

    async def _fake_classify(self, **kwargs):
        # Simulate a peer retaking the expired claim while this run is in
        # flight: the stamp this call wrote is replaced by the peer's.
        async with ah._trial_session(trial_id) as (_s, t):
            t.analysis_started_at = utcnow()
        self.last_usage = _usage()
        return _fake_classification()

    monkeypatch.setattr(TrialClassifier, "classify_trial", _fake_classify)
    _stub_directories(monkeypatch, ah)

    try:
        status = await ah.classify_trial_and_store(trial_id)

        await session.rollback()
        trial = await session.get(TrialModel, trial_id)
        await session.refresh(trial)
        assert trial.analysis is None, "a superseded run overwrote the current owner"
        assert trial.analysis_status == AnalysisStatus.RUNNING
        # The peer is still working, so the caller must keep waiting. Reporting
        # the FAILED initializer here would let _classify_waiting_out_peer_claim
        # return and QA synthesize a verdict without this trial.
        assert status == AnalysisStatus.RUNNING
    finally:
        await _cleanup(
            session, task_id=task_id, experiment_id=experiment_id, trial_id=trial_id
        )


@pytest.mark.asyncio
async def test_late_store_does_not_resurrect_a_cancelled_trial(session, monkeypatch):
    """A cancel that lands mid-classification must stay terminal.

    Cancelling stamps ``analysis_status = FAILED`` but leaves
    ``analysis_started_at`` untouched (``queue.cancel_task``,
    ``core.endpoints.qa``), so the claim stamp still matches this run. Only the
    status check stops the shielded late write from flipping the trial back to
    SUCCESS and undoing the user's cancel.
    """
    from oddish.workers.queue import analysis_handler as ah

    trial_id = f"trial-cancel-{uuid.uuid4().hex[:8]}"
    experiment_id = f"exp-{uuid.uuid4().hex[:8]}"
    task_id = await _seed(session, trial_id, experiment_id=experiment_id)

    async def _fake_classify(self, **kwargs):
        # Cancel lands while the classifier is running: terminal status, same
        # analysis_started_at.
        async with ah._trial_session(trial_id) as (_s, t):
            t.analysis_status = AnalysisStatus.FAILED
            t.analysis_error = "Cancelled by user"
            t.analysis_finished_at = utcnow()
        self.last_usage = _usage()
        return _fake_classification()

    monkeypatch.setattr(TrialClassifier, "classify_trial", _fake_classify)
    _stub_directories(monkeypatch, ah)

    try:
        status = await ah.classify_trial_and_store(trial_id)

        await session.rollback()
        trial = await session.get(TrialModel, trial_id)
        await session.refresh(trial)
        assert trial.analysis_status == AnalysisStatus.FAILED, (
            "a late store resurrected a cancelled trial"
        )
        assert trial.analysis is None
        assert trial.analysis_error == "Cancelled by user"
        # Terminal, so the caller must not wait for a peer that will never come.
        assert status == AnalysisStatus.FAILED
    finally:
        await _cleanup(
            session, task_id=task_id, experiment_id=experiment_id, trial_id=trial_id
        )
