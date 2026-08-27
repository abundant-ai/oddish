"""Focused contracts for historical QA prompt replay."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddish.core.endpoints.qa_eval import create_qa_eval_core
from oddish.db import AnalysisStatus, TaskModel, TaskVersionModel, TrialStatus
from oddish.db.models import TrialModel
from oddish.schemas import QAEvalCreateRequest
from oddish.worker.analysis_result_check import check_analysis_result
from oddish.workers.analysis_trials import (
    _import_qa_eval_result,
    analysis_check_payload,
    build_qa_eval_brief,
    is_analysis_kind,
)
from oddish.workers.queue.cleanup import (
    STALE_ANALYSIS_IMPORT_BATCH_LIMIT,
    _heal_stale_qa_eval_imports,
)


def _artifact(source_trial_id: str = "source-1") -> dict:
    return {
        "source_trial_id": source_trial_id,
        "classification": "GOOD_FAILURE",
        "subtype": "misdiagnosis",
        "evidence": "- The agent changed the wrong file (trajectory step 4).",
        "root_cause": "The agent diagnosed the wrong component.",
        "recommendation": "N/A",
        "action_items": [],
        "exploitation": [],
    }


def test_request_strips_and_deduplicates_source_trial_ids():
    request = QAEvalCreateRequest(
        name=" replay ",
        source_trial_ids=[" source-1 ", "source-1", "source-2"],
        prompt_name=" candidate-1 ",
        prompt_text=" classify this ",
        model=" ",
    )
    assert request.name == "replay"
    assert request.source_trial_ids == ["source-1", "source-2"]
    assert request.prompt_name == "candidate-1"
    assert request.prompt_text == "classify this"
    assert request.model is None


def test_qa_eval_is_an_analysis_kind_and_brief_pins_one_source():
    assert is_analysis_kind("qa_eval")
    brief = build_qa_eval_brief(
        task_name="demo",
        source_trial_id="source-1",
        candidate_prompt="Candidate rules go here.",
        pre_trial_items=[{"id": "finding-1"}],
    )
    assert "source-1" in brief
    assert "Candidate rules go here." in brief
    assert "finding-1" in brief
    assert "qa_eval_result.json" in brief
    assert "trials result source-1 > /tmp/source-result.json" in brief
    assert "trials trajectory source-1 > /tmp/source-trajectory.json" in brief
    assert "trials logs source-1 > /tmp/source-logs.json" in brief
    assert "task fetch --into /tmp/source-task" in brief
    assert "Do not use or copy the source trial's existing `analysis`" in brief


def test_qa_eval_validator_requires_exact_source_and_complete_analysis():
    expected = analysis_check_payload(
        "qa_eval", {"analysis_payload": {"source_trial_id": "source-1"}}
    )
    assert check_analysis_result(_artifact(), expected) == []

    wrong_source = _artifact("source-2")
    assert any(
        "source_trial_id" in error
        for error in check_analysis_result(wrong_source, expected)
    )
    missing_root_cause = _artifact()
    del missing_root_cause["root_cause"]
    assert any(
        "root_cause" in error
        for error in check_analysis_result(missing_root_cause, expected)
    )


@pytest.mark.asyncio
async def test_create_does_not_link_replay_as_a_source_task_experiment(monkeypatch):
    source = TrialModel(
        id="source-1",
        name="source-1",
        task_id="task-1",
        task_version_id="version-1",
        experiment_id="original-experiment",
        org_id="org-1",
        agent="codex",
        provider="openai",
        queue_key="codex:model",
        model="model",
        kind="agent",
        status=TrialStatus.SUCCESS,
        is_probe=False,
        has_trajectory=True,
        analysis={"classification": "BAD_FAILURE"},
    )
    task = TaskModel(
        id="task-1",
        name="task-1",
        org_id="org-1",
        current_version_id="version-1",
    )
    version = TaskVersionModel(
        id="version-1",
        task_id="task-1",
        task_s3_key="tasks/task-1/version-1.tar.gz",
    )

    class FakeResult:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class FakeSession:
        added_experiment = None

        async def execute(self, statement):
            descriptions = getattr(statement, "column_descriptions", None)
            if not descriptions:
                raise AssertionError(
                    "QA-eval creation must not write task_experiments membership"
                )
            entity = descriptions[0].get("entity")
            rows_by_entity = {
                TrialModel: [source],
                TaskModel: [task],
                TaskVersionModel: [version],
            }
            return FakeResult(rows_by_entity[entity])

        def add(self, row):
            self.added_experiment = row

        async def flush(self):
            self.added_experiment.id = "replay-experiment"

    async def fake_create_analysis_trial(_session, **kwargs):
        assert kwargs["task"] is task
        assert kwargs["experiment_id"] == "replay-experiment"
        return SimpleNamespace(id="qa-eval-1")

    monkeypatch.setattr(
        "oddish.core.endpoints.qa_eval.create_analysis_trial",
        fake_create_analysis_trial,
    )

    response = await create_qa_eval_core(
        FakeSession(),
        request=QAEvalCreateRequest(
            name="candidate replay",
            source_trial_ids=["source-1"],
            prompt_name="candidate-1",
            prompt_text="Classify the stored solver evidence.",
        ),
        org_id="org-1",
        owner_user_id="user-1",
    )

    assert response.experiment_id == "replay-experiment"
    assert response.trials[0].qa_eval_trial_id == "qa-eval-1"


@pytest.mark.asyncio
async def test_importer_retries_storage_errors_and_writes_only_the_eval_trial(
    monkeypatch,
):
    eval_trial = TrialModel(
        id="eval-1",
        name="eval-1",
        task_id="task-1",
        agent="claude-code",
        provider="anthropic",
        queue_key="claude-code:model",
        model="model",
        kind="qa_eval",
        status=TrialStatus.SUCCESS,
        harbor_config={"analysis_payload": {"source_trial_id": "source-1"}},
    )
    source_snapshot = {
        "analysis": {"classification": "BAD_FAILURE"},
        "reward": 0.0,
        "result": {"reward": 0.0},
    }

    class FakeSession:
        requested_ids: list[str] = []

        async def get(self, _model, trial_id):
            self.requested_ids.append(trial_id)
            return eval_trial if trial_id == eval_trial.id else None

    fake_session = FakeSession()

    @asynccontextmanager
    async def fake_get_session():
        yield fake_session

    read_attempts = 0

    async def fake_read_artifact(_trial, _filename):
        nonlocal read_attempts
        read_attempts += 1
        if read_attempts == 1:
            raise OSError("storage temporarily unavailable")
        return _artifact()

    monkeypatch.setattr("oddish.workers.analysis_trials.get_session", fake_get_session)
    monkeypatch.setattr(
        "oddish.workers.analysis_trials.read_analysis_artifact", fake_read_artifact
    )

    with pytest.raises(OSError, match="storage temporarily unavailable"):
        await _import_qa_eval_result(eval_trial)

    assert fake_session.requested_ids == []
    assert eval_trial.analysis is None
    assert eval_trial.analysis_status is None

    await _import_qa_eval_result(eval_trial)

    assert fake_session.requested_ids == ["eval-1"]
    assert eval_trial.analysis == _artifact()
    assert eval_trial.analysis_status == AnalysisStatus.SUCCESS
    assert source_snapshot == {
        "analysis": {"classification": "BAD_FAILURE"},
        "reward": 0.0,
        "result": {"reward": 0.0},
    }


@pytest.mark.asyncio
async def test_cleanup_scan_requeues_unfinished_qa_eval_imports():
    result = SimpleNamespace(all=lambda: [("qa-eval-1",)])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    assert await _heal_stale_qa_eval_imports(session) == ["qa-eval-1"]
    statement, params = session.execute.await_args.args
    sql = str(statement)
    assert "tr.kind = 'qa_eval'" in sql
    assert "tr.status::text IN ('SUCCESS', 'FAILED', 'SKIPPED')" in sql
    assert "tr.analysis_status IS NULL" in sql
    assert params == {"batch_limit": STALE_ANALYSIS_IMPORT_BATCH_LIMIT}
    assert STALE_ANALYSIS_IMPORT_BATCH_LIMIT == 200
