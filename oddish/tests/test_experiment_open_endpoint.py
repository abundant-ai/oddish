from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.experiment_page import (
    OPEN_MAX_BYTES,
    OPEN_MAX_TASKS,
    _TRIAL_PAGE_COLUMNS,
    get_experiment_open_core,
    get_experiment_trial_page_core,
)
from oddish.core.cost_exclusions import CostExclusions
from oddish.core.helpers import experiment_effective_versions_selectable
from oddish.db import TrialOrigin, TrialStatus
from oddish.db.models import TrialModel

NOW = datetime(2026, 8, 26, tzinfo=UTC)


class _Mappings:
    def __init__(self, rows):
        self.rows = list(rows)

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self):
        return self.rows


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return _Mappings(self.rows)

    def all(self):
        return self.rows


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def execute(self, query):
        self.calls.append(query)
        if not self.results:
            raise AssertionError("unexpected query")
        return self.results.pop(0)


def _identity():
    return {
        "id": "experiment-1",
        "name": "Bounded experiment",
        "created_at": NOW,
        "owner": "octocat",
        "link": "https://github.com/acme/repo/pull/1",
        "revision": NOW + timedelta(minutes=1),
    }


def _summary(**overrides):
    values = {
        "task_count": 2,
        "total": 7,
        "completed": 4,
        "failed": 1,
        "skipped": 1,
        "pass_count": 3,
        "partial_count": 1,
        "fail_count": 0,
        "reward_sum": 3.5,
        "reward_total": 4,
        "average_score": 0.75,
        "qa_accepted": 1,
        "qa_rejected": 0,
        "qa_running": 1,
        "qa_failed": 0,
        "has_active_trials": True,
    }
    values.update(overrides)
    return values


def _task(index: int, **overrides):
    values = {
        "task_id": f"task-{index:03}",
        "name": f"Task {index}",
        "status": "running",
        "priority": "low",
        "user": "octocat",
        "task_path": f"tasks/task-{index:03}",
        "current_version_id": f"task-{index:03}-v2",
        "current_version": 2,
        "trial_version_id": f"task-{index:03}-v1",
        "trial_version": 1,
        "run_analysis": True,
        "verdict_status": "success",
        "verdict_label": "accept",
        "verdict_is_good": "true",
        "verdict_confidence": "high",
        "verdict_error": None,
        "created_at": NOW - timedelta(seconds=index),
        "updated_at": NOW,
        "total": 4,
        "completed": 2,
        "failed": 1,
        "skipped": 1,
        "pass_count": 2,
        "partial_count": 0,
        "fail_count": 0,
        "reward_sum": 2.0,
        "reward_total": 2,
        "average_score": 1.0,
    }
    values.update(overrides)
    return values


def _trial(index: int) -> TrialModel:
    return TrialModel(
        id=f"trial-{index:03}",
        name=f"trial-{index:03}",
        task_id="task-001",
        task_version_id="task-001-v1",
        experiment_id="experiment-1",
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.6",
        model="openai/gpt-5.6",
        status=TrialStatus.SUCCESS,
        origin=TrialOrigin.ODDISH,
        attempts=1,
        max_attempts=6,
        harbor_stage="completed",
        reward=1.0,
        is_probe=False,
        kind="agent",
        input_tokens=100,
        cache_tokens=20,
        cache_write_tokens=0,
        output_tokens=10,
        cost_usd=0.01,
        has_trajectory=True,
        created_at=NOW - timedelta(seconds=index),
        started_at=NOW - timedelta(minutes=1),
        finished_at=NOW,
    )


def _trial_page_row(
    trial: TrialModel,
    *,
    classification=None,
    subtype=None,
    evidence=None,
):
    row = {column.key: getattr(trial, column.key) for column in _TRIAL_PAGE_COLUMNS}
    row.update(
        task_path="tasks/task-001",
        analysis_classification=classification,
        analysis_subtype=subtype,
        analysis_evidence=evidence,
    )
    return row


def _open(*tasks, summary=None, **kwargs):
    session = _Session(
        _Result([_identity()]),
        _Result([summary or _summary()]),
        _Result(tasks),
    )
    response = asyncio.run(
        get_experiment_open_core(
            session,
            experiment_id="experiment-1",
            org_id="org-1",
            **kwargs,
        )
    )
    return session, response


def _sql(query) -> str:
    return str(
        query.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_experiment_open_is_exact_compact_bounded_and_three_queries():
    session, response = _open(_task(1), _task(2))

    assert len(session.calls) == 3
    assert response.experiment_id == "experiment-1"
    assert response.revision == NOW + timedelta(minutes=1)
    assert response.has_active_trials is True
    assert response.summary.model_dump() == {
        "task_count": 2,
        "trial_count": 7,
        "completed": 4,
        "failed": 1,
        "skipped": 1,
        "active": 1,
        "reward_sum": 3.5,
        "reward_total": 4,
        "pass_count": 3,
        "partial_count": 1,
        "fail_count": 0,
        "harness_error_count": 1,
        "average_score": 0.75,
        "qa_accepted": 1,
        "qa_rejected": 0,
        "qa_running": 1,
        "qa_failed": 0,
    }
    assert response.tasks[0].status == "completed"
    assert response.tasks[0].current_version_id == "task-001-v2"
    assert response.tasks[0].trial_version_id == "task-001-v1"
    assert response.tasks[0].verdict is not None
    assert response.tasks[0].verdict.verdict == "accept"
    assert len(response.model_dump_json().encode()) < OPEN_MAX_BYTES


def test_experiment_open_caps_rows_and_returns_a_stable_boundary():
    rows = [_task(index) for index in range(OPEN_MAX_TASKS + 1)]
    _, response = _open(
        *rows,
        summary=_summary(task_count=len(rows), total=0, completed=0, failed=0),
    )

    assert 0 < len(response.tasks) <= OPEN_MAX_TASKS
    last_index = len(response.tasks) - 1
    assert response.next_created_at == rows[last_index]["created_at"]
    assert response.next_task_id == rows[last_index]["task_id"]
    assert len(response.model_dump_json().encode()) < OPEN_MAX_BYTES


def test_experiment_open_rejects_one_task_shell_over_the_byte_budget():
    oversized = _task(1, task_path="x" * OPEN_MAX_BYTES)
    session = _Session(
        _Result([_identity()]), _Result([_summary(task_count=1)]), _Result([oversized])
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_experiment_open_core(
                session, experiment_id="experiment-1", org_id="org-1"
            )
        )

    assert exc.value.status_code == 413
    assert len(session.calls) == 3


def test_experiment_open_requires_both_typed_page_fields_before_querying():
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_experiment_open_core(
                session,
                experiment_id="experiment-1",
                org_id="org-1",
                before_created_at=NOW,
            )
        )
    assert exc.value.status_code == 400
    assert session.calls == []


def test_experiment_open_missing_or_cross_org_stops_after_access_query():
    session = _Session(_Result([]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_experiment_open_core(session, experiment_id="missing", org_id="org-1")
        )
    assert exc.value.status_code == 404
    assert len(session.calls) == 1
    access_sql = _sql(session.calls[0])
    assert "experiments.org_id = 'org-1'" in access_sql
    assert "experiments.deleted_at IS NULL" in access_sql


def test_experiment_open_sql_reuses_visibility_and_version_rules():
    session, _ = _open(_task(1))
    summary_sql = _sql(session.calls[1])
    page_sql = _sql(session.calls[2])

    for sql in (summary_sql, page_sql):
        assert "experiment_trials" in sql
        assert "trials.kind = 'agent'" in sql
        assert "trials.is_probe IS false" in sql
        assert "trials.superseded_by_trial_id IS NULL" in sql
        assert "trials.deleted_at IS NULL" in sql
        assert "row_number() OVER" in sql
        assert "task_versions.version DESC" in sql
    for heavy_column in (
        "trials.result",
        "trials.analysis",
        "trials.phase_timing",
        "trials.harbor_config",
        "trials.error_message",
    ):
        assert heavy_column not in page_sql


def test_effective_version_selectable_filters_before_ranking():
    sql = _sql(
        experiment_effective_versions_selectable(
            experiment_id="experiment-1", task_ids=["task-1", "task-2"]
        )
    )
    assert "trials.task_id IN ('task-1', 'task-2')" in sql
    assert "trials.task_version_id = tasks.current_version_id" in sql
    assert "task_versions.version DESC" in sql
    assert "experiment_version_candidates.rank = 1" in sql


def test_trial_page_is_flat_bounded_and_omits_detail_columns(monkeypatch):
    async def no_exclusions(_session):
        return CostExclusions()

    monkeypatch.setattr(
        "oddish.core.endpoints.experiment_page.load_cost_exclusions", no_exclusions
    )
    first, second = _trial(1), _trial(2)
    session = _Session(
        _Result([_identity()]),
        _Result(
            [
                _trial_page_row(
                    first,
                    classification="GOOD_SUCCESS",
                    subtype="correct",
                    evidence="evidence",
                ),
                _trial_page_row(second),
            ]
        ),
    )
    response = asyncio.run(
        get_experiment_trial_page_core(
            session,
            experiment_id="experiment-1",
            org_id="org-1",
            limit=1,
        )
    )

    assert len(session.calls) == 2
    assert [trial.id for trial in response.trials] == [first.id]
    assert response.trials[0].has_trajectory is True
    assert response.trials[0].analysis.classification == "GOOD_SUCCESS"
    assert response.next_created_at == first.created_at
    assert response.next_trial_id == first.id
    payload = response.model_dump_json()
    for field in ("result", "phase_timing", "harbor_config", "error_message"):
        assert f'"{field}"' not in payload

    sql = _sql(session.calls[1])
    assert "LIMIT 2" in sql
    assert "trials.kind = 'agent'" in sql
    assert "trials.is_probe IS false" in sql
    assert "trials.superseded_by_trial_id IS NULL" in sql
    assert "trials.result" not in sql
    assert "trials.phase_timing" not in sql
    assert "trials.harbor_config" not in sql
    assert "trials.error_message" not in sql
    assert "left(trials.analysis ->> 'classification', 100)" in sql


def test_trial_page_requires_both_page_fields_before_querying():
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_experiment_trial_page_core(
                session,
                experiment_id="experiment-1",
                org_id="org-1",
                before_created_at=NOW,
            )
        )
    assert exc.value.status_code == 400
    assert session.calls == []
