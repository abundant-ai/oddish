from __future__ import annotations

import types
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from oddish.analyze import TrialClassifier
from oddish.analyze.analysis_cost import AnalysisUsage
from oddish.db import ExperimentModel, TaskModel, TaskStatus, TrialModel
from oddish.db.models import AnalysisCostModel


async def _seed_task_and_trial(
    session, trial_id: str, *, org_id: str, experiment_id: str, billed_user_id: str
) -> str:
    """Insert an Experiment/Task/Trial and commit so the handler's own
    sessions can see them. Returns the generated task_id for cleanup.
    """
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/cost-ledger-fake-task",
            status=TaskStatus.COMPLETED,
        )
    )
    session.add(
        TrialModel(
            id=trial_id,
            name=trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id=org_id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            model="gpt-5",
            is_probe=False,
            max_attempts=6,
            billed_user_id=billed_user_id,
        )
    )
    await session.flush()
    await session.commit()
    return task_id


async def _cleanup(session, *, task_id: str, experiment_id: str, trial_id: str) -> None:
    """Undo the commit in _seed_task_and_trial (fixture rollback can't)."""
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
        trial_name="trial-cost-test-1",
        classification=types.SimpleNamespace(value="GOOD"),
        subtype="none",
        evidence="looks fine",
        root_cause=None,
        recommendation=None,
        reward=1.0,
    )


def _stub_directory_resolution(monkeypatch, ah):
    async def _fake_resolve_task_directory(**kwargs):
        return Path("/tmp"), None, "key"

    async def _fake_resolve_trial_directory(**kwargs):
        return Path("/tmp"), None, "key"

    monkeypatch.setattr(ah, "resolve_task_directory", _fake_resolve_task_directory)
    monkeypatch.setattr(ah, "resolve_trial_directory", _fake_resolve_trial_directory)


@pytest.mark.asyncio
async def test_successful_classification_writes_one_cost_row(session, monkeypatch):
    from oddish.workers.queue import analysis_handler as ah

    trial_id = "trial-cost-test-1"
    task_id = await _seed_task_and_trial(session, trial_id, org_id="org-x",
                               experiment_id="exp-x", billed_user_id="user-x")

    # Stub the classifier so no real subprocess runs, and set last_usage.
    async def _fake_classify(self, *, trial_dir, task_dir, trial_agent):
        self.last_usage = AnalysisUsage(
            cost_usd=0.05, input_tokens=100, output_tokens=20,
            cache_read_tokens=0, cache_write_tokens=0,
            model="anthropic/claude-sonnet-4", source="native",
        )
        return _fake_classification()

    monkeypatch.setattr(TrialClassifier, "classify_trial", _fake_classify)
    _stub_directory_resolution(monkeypatch, ah)

    try:
        await ah.classify_trial_and_store(trial_id)

        rows = (await session.execute(
            select(AnalysisCostModel).where(AnalysisCostModel.trial_id == trial_id)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].job_kind == "trial_classifier"
        assert rows[0].cost_usd == 0.05
        assert rows[0].org_id == "org-x"
        assert rows[0].experiment_id == "exp-x"
        assert rows[0].billed_user_id == "user-x"
    finally:
        await _cleanup(session, task_id=task_id, experiment_id="exp-x", trial_id=trial_id)


@pytest.mark.asyncio
async def test_failed_classification_writes_no_cost_row(session, monkeypatch):
    from oddish.workers.queue import analysis_handler as ah

    trial_id = "trial-cost-test-2"
    task_id = await _seed_task_and_trial(session, trial_id, org_id="org-x",
                               experiment_id="exp-x", billed_user_id="user-x")

    async def _raise(self, *, trial_dir, task_dir, trial_agent):
        raise RuntimeError("boom")

    monkeypatch.setattr(TrialClassifier, "classify_trial", _raise)
    _stub_directory_resolution(monkeypatch, ah)

    try:
        await ah.classify_trial_and_store(trial_id)

        rows = (await session.execute(
            select(AnalysisCostModel).where(AnalysisCostModel.trial_id == trial_id)
        )).scalars().all()
        assert rows == []
    finally:
        await _cleanup(session, task_id=task_id, experiment_id="exp-x", trial_id=trial_id)
