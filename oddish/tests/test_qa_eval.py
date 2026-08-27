"""Focused contracts for historical QA prompt replay."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from oddish.db import AnalysisStatus, TrialStatus
from oddish.db.models import TrialModel
from oddish.schemas import QAEvalCreateRequest
from oddish.worker.analysis_result_check import check_analysis_result
from oddish.workers.analysis_trials import (
    _import_qa_eval_result,
    analysis_check_payload,
    build_qa_eval_brief,
    is_analysis_kind,
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
async def test_importer_writes_only_the_evaluation_trial(monkeypatch):
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

    async def fake_read_artifact(_trial, _filename):
        return _artifact()

    monkeypatch.setattr(
        "oddish.workers.analysis_trials.get_session", fake_get_session
    )
    monkeypatch.setattr(
        "oddish.workers.analysis_trials.read_analysis_artifact", fake_read_artifact
    )

    await _import_qa_eval_result(eval_trial)

    assert fake_session.requested_ids == ["eval-1"]
    assert eval_trial.analysis == _artifact()
    assert eval_trial.analysis_status == AnalysisStatus.SUCCESS
    assert source_snapshot == {
        "analysis": {"classification": "BAD_FAILURE"},
        "reward": 0.0,
        "result": {"reward": 0.0},
    }
