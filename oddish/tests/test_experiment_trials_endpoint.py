from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import endpoints  # noqa: E402
from oddish.db import ExperimentModel  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *, experiment=None, rows=None):
        self.experiment = experiment
        self.rows = rows or []
        self.get_calls = []
        self.statements = []

    async def get(self, model, obj_id):
        self.get_calls.append((model, obj_id))
        return self.experiment

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)


def _trial(trial_id: str, *, task_id: str = "task-1"):
    return SimpleNamespace(id=trial_id, task_id=task_id)


def _patch_response_dependencies(monkeypatch):
    async def fake_fetch_trial_queue_info(session, *, trials):
        return {trial.id: f"queue:{trial.id}" for trial in trials}

    async def fake_fetch_visible_worker_jobs(session, *, trial_ids):
        return {("trials", trial_id): [f"job:{trial_id}"] for trial_id in trial_ids}

    def fake_build_trial_response(trial, task_path, *, queue_info=None, jobs=None):
        return {
            "id": trial.id,
            "task_id": trial.task_id,
            "task_path": task_path,
            "queue_info": queue_info,
            "jobs": jobs or [],
        }

    monkeypatch.setattr(
        endpoints, "fetch_trial_queue_info", fake_fetch_trial_queue_info
    )
    monkeypatch.setattr(
        endpoints, "fetch_visible_worker_jobs", fake_fetch_visible_worker_jobs
    )
    monkeypatch.setattr(endpoints, "build_trial_response", fake_build_trial_response)


@pytest.mark.asyncio
async def test_list_experiment_trials_returns_direct_experiment_trials(monkeypatch):
    _patch_response_dependencies(monkeypatch)
    session = _Session(
        experiment=SimpleNamespace(id="exp-a"),
        rows=[
            (_trial("task-1-0"), "/tasks/task-1-v1"),
            (_trial("task-1-1"), "/tasks/task-1-v1"),
            (_trial("task-1-2"), "/tasks/task-1-v2"),
        ],
    )

    trials = await endpoints.list_experiment_trials_core(
        session, experiment_id="exp-a", limit=1000, offset=0
    )

    assert [trial["id"] for trial in trials] == ["task-1-0", "task-1-1", "task-1-2"]
    assert [trial["task_path"] for trial in trials] == [
        "/tasks/task-1-v1",
        "/tasks/task-1-v1",
        "/tasks/task-1-v2",
    ]
    assert trials[0]["queue_info"] == "queue:task-1-0"
    assert trials[0]["jobs"] == ["job:task-1-0"]
    assert session.get_calls == [(ExperimentModel, "exp-a")]


@pytest.mark.asyncio
async def test_list_experiment_trials_filters_by_experiment_and_paginates(monkeypatch):
    _patch_response_dependencies(monkeypatch)
    session = _Session(experiment=SimpleNamespace(id="exp-a"))

    await endpoints.list_experiment_trials_core(
        session, experiment_id="exp-a", limit=2, offset=3
    )

    statement = session.statements[0]
    compiled = statement.compile()
    assert "trials.experiment_id" in str(compiled)
    assert "exp-a" in compiled.params.values()
    assert statement._limit_clause.value == 2
    assert statement._offset_clause.value == 3
    assert [str(clause) for clause in statement._order_by_clauses] == [
        "trials.created_at ASC",
        "trials.id ASC",
    ]


@pytest.mark.asyncio
async def test_list_experiment_trials_returns_empty_for_experiment_without_trials(
    monkeypatch,
):
    _patch_response_dependencies(monkeypatch)
    session = _Session(experiment=SimpleNamespace(id="exp-empty"), rows=[])

    trials = await endpoints.list_experiment_trials_core(
        session, experiment_id="exp-empty"
    )

    assert trials == []


@pytest.mark.asyncio
async def test_list_experiment_trials_raises_404_for_missing_experiment():
    session = _Session(experiment=None)

    with pytest.raises(HTTPException) as exc:
        await endpoints.list_experiment_trials_core(session, experiment_id="missing")

    assert exc.value.status_code == 404
    assert "Experiment missing not found" in exc.value.detail
    assert session.statements == []
