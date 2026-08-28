"""Focused contracts for pointer-based historical QA replay."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from oddish.core.endpoints.qa_eval import create_qa_eval_core
from oddish.db import AnalysisStatus, TaskModel, TaskVersionModel, TrialStatus
from oddish.db.models import TrialModel
from oddish.schemas import QAEvalCreateRequest
from oddish.worker.analysis_result_check import check_analysis_result
from oddish.workers.analysis_trials import (
    _import_qa_eval_result,
    analysis_check_payload,
    build_qa_brief,
    is_analysis_kind,
)


def _analysis() -> dict:
    return {
        "trial_name": "source-1",
        "classification": "GOOD_FAILURE",
        "subtype": "misdiagnosis",
        "evidence": "The agent changed the wrong file.",
        "root_cause": "The agent diagnosed the wrong component.",
        "recommendation": "N/A",
        "reward": 0.0,
        "action_items": [],
        "exploitation": [],
    }


def _qa_artifact(source_trial_id: str = "source-1") -> dict:
    return {
        "trials": [
            {
                "trial_id": source_trial_id,
                "analysis": _analysis(),
                "trajectory_summary": {
                    "summary": "The agent edited the wrong file.",
                    "highlights": [],
                    "components": [
                        {
                            "step_ids": [1],
                            "trajectory_component": "implementing",
                            "action": "edit",
                            "purpose": "build",
                            "summary": "One edit.",
                        }
                    ],
                },
            }
        ],
        "verdict": None,
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


def test_replay_reuses_the_production_qa_brief_and_contract():
    assert is_analysis_kind("qa_eval")
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["source-1"],
        pre_trial_items=[{"id": "finding-1"}],
        with_verdict=False,
        classification_prompt="Candidate rules go here.",
    )
    assert "source-1" in brief
    assert "Candidate rules go here." in brief
    assert "finding-1" in brief
    assert "qa_result.json" in brief
    assert '"verdict": null' in brief

    expected = analysis_check_payload(
        "qa_eval",
        {
            "analysis_payload": {
                "trial_ids": ["source-1"],
                "with_verdict": False,
            }
        },
    )
    assert expected["kind"] == "qa"
    assert check_analysis_result(_qa_artifact(), expected) == []


def _source(
    *,
    has_trajectory: bool = True,
    status: TrialStatus = TrialStatus.SUCCESS,
) -> TrialModel:
    return TrialModel(
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
        status=status,
        is_probe=False,
        has_trajectory=has_trajectory,
        analysis={"classification": "BAD_FAILURE"},
    )


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _CreateSession:
    def __init__(self, source: TrialModel):
        self.source = source
        self.added_experiment = None
        self.task = TaskModel(
            id="task-1",
            name="task-1",
            org_id="org-1",
            current_version_id="version-1",
        )
        self.version = TaskVersionModel(
            id="version-1",
            task_id="task-1",
            task_s3_key="tasks/task-1/version-1.tar.gz",
        )

    async def execute(self, statement):
        descriptions = getattr(statement, "column_descriptions", None)
        if not descriptions:
            raise AssertionError("replay must not write task_experiments membership")
        entity = descriptions[0].get("entity")
        return _Result(
            {
                TrialModel: [self.source],
                TaskModel: [self.task],
                TaskVersionModel: [self.version],
            }[entity]
        )

    def add(self, row):
        self.added_experiment = row

    async def flush(self):
        self.added_experiment.id = "replay-experiment"


@pytest.mark.asyncio
async def test_create_accepts_a_failed_source_without_a_trajectory(monkeypatch):
    source = _source(has_trajectory=False, status=TrialStatus.FAILED)
    session = _CreateSession(source)
    captured = {}
    admitted = []

    async def fake_admit_trials(_session, org_id, billed_user_id, count):
        admitted.append((org_id, billed_user_id, count))

    async def fake_create_analysis_trial(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="qa-eval-1")

    monkeypatch.setattr(
        "oddish.core.endpoints.qa_eval.create_analysis_trial",
        fake_create_analysis_trial,
    )
    monkeypatch.setattr(
        "oddish.core.endpoints.qa_eval.admit_trials",
        fake_admit_trials,
    )
    response = await create_qa_eval_core(
        session,
        request=QAEvalCreateRequest(
            name="candidate replay",
            source_trial_ids=["source-1"],
            prompt_name="candidate-1",
            prompt_text="Classify the stored solver evidence.",
        ),
        org_id="org-1",
        owner_user_id="user-1",
        billed_user_id="user-1",
    )

    assert response.experiment_id == "replay-experiment"
    assert response.trials[0].qa_eval_trial_id == "qa-eval-1"
    assert captured["experiment_id"] == "replay-experiment"
    assert captured["task_version_id"] == "version-1"
    assert captured["billed_user_id"] == "user-1"
    assert captured["payload"]["trial_ids"] == ["source-1"]
    assert captured["payload"]["trial_evidence"] == [
        {
            "trial_id": "source-1",
            "status": "failed",
            "reward": None,
            "has_trajectory": False,
            "agent": "codex",
            "baseline_kind": None,
        }
    ]
    assert captured["payload"]["pre_trial_item_ids"] == []
    assert captured["payload"]["pre_trial_must_fix_ids"] == []
    assert captured["payload"]["baseline_evidence"] == []
    assert "source_trial_id" not in captured["payload"]
    assert admitted == [("org-1", "user-1", 1)]
    assert source.experiment_id == "original-experiment"
    assert source.analysis == {"classification": "BAD_FAILURE"}


@pytest.mark.asyncio
async def test_create_rejects_the_whole_request_when_a_source_is_ineligible():
    session = _CreateSession(_source(status=TrialStatus.RUNNING))
    with pytest.raises(HTTPException, match=r"not terminal \(running\)"):
        await create_qa_eval_core(
            session,
            request=QAEvalCreateRequest(
                name="candidate replay",
                source_trial_ids=["source-1"],
                prompt_name="candidate-1",
                prompt_text="Classify the stored solver evidence.",
            ),
            org_id="org-1",
            owner_user_id="user-1",
            billed_user_id="user-1",
        )
    assert session.added_experiment is None


@pytest.mark.asyncio
async def test_importer_extracts_one_standard_qa_analysis(monkeypatch):
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
        harbor_config={
            "analysis_payload": {
                "trial_ids": ["source-1"],
                "with_verdict": False,
            }
        },
    )

    class FakeSession:
        async def get(self, _model, trial_id):
            if trial_id == "eval-1":
                return eval_trial
            if trial_id == "source-1":
                return _source()
            return None

    @asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    async def fake_read_artifact(_trial, filename):
        assert filename == "qa_result.json"
        return _qa_artifact()

    monkeypatch.setattr("oddish.workers.analysis_trials.get_session", fake_get_session)
    monkeypatch.setattr(
        "oddish.workers.analysis_trials.read_analysis_artifact", fake_read_artifact
    )

    await _import_qa_eval_result(eval_trial)
    assert eval_trial.analysis == {
        **_analysis(),
        "trial_name": "source-1",
        "reward": None,
    }
    assert eval_trial.analysis_status == AnalysisStatus.SUCCESS


@pytest.mark.asyncio
async def test_importer_requires_exactly_one_source_trial(monkeypatch):
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
        harbor_config={
            "analysis_payload": {
                "trial_ids": ["source-1", "source-2"],
                "with_verdict": False,
            }
        },
    )

    class FakeSession:
        async def get(self, _model, trial_id):
            return eval_trial if trial_id == "eval-1" else None

    @asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    async def fake_read_artifact(_trial, _filename):
        return _qa_artifact()

    monkeypatch.setattr("oddish.workers.analysis_trials.get_session", fake_get_session)
    monkeypatch.setattr(
        "oddish.workers.analysis_trials.read_analysis_artifact", fake_read_artifact
    )

    await _import_qa_eval_result(eval_trial)

    assert eval_trial.analysis is None
    assert eval_trial.analysis_status == AnalysisStatus.FAILED
    assert "exactly one" in eval_trial.analysis_error
