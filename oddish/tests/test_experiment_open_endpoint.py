from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.experiment_page import (
    OPEN_MAX_BYTES,
    OPEN_MAX_TASKS,
    _TRIAL_PAGE_COLUMNS,
    get_experiment_focus_core,
    get_experiment_open_core,
    get_experiment_trial_page_core,
    get_public_experiment_open_core,
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

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


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
        "tags": {},
        "current_version_id": f"task-{index:03}-v2",
        "current_version": 2,
        "trial_version_id": f"task-{index:03}-v1",
        "trial_version": 1,
        "run_analysis": True,
        "verdict_status": "success",
        "verdict_label": "accept",
        "verdict_is_good": "true",
        "verdict_confidence": "high",
        "verdict_primary_issue": None,
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
    page_ids = [
        {"task_id": task["task_id"], "created_at": task["created_at"]} for task in tasks
    ]
    session = _Session(
        _Result([_identity()]),
        _Result(page_ids),
        _Result(tasks),
        _Result([summary or _summary()]),
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


def test_experiment_open_is_exact_compact_bounded_and_four_queries():
    session, response = _open(
        _task(
            1,
            tags={"github_meta": '{"category":"JS","world":"World_7","domain":"Law"}'},
        ),
        _task(2),
    )

    assert len(session.calls) == 4
    assert response.experiment_id == "experiment-1"
    assert response.revision == NOW + timedelta(minutes=1)
    assert response.has_active_trials is True
    assert response.summary is not None
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
    assert response.tasks[0].github_meta == {
        "category": "JS",
        "world": "World_7",
        "domain": "Law",
    }
    assert response.tasks[0].verdict is not None
    assert response.tasks[0].verdict.verdict == "accept"
    assert len(response.model_dump_json().encode()) < OPEN_MAX_BYTES


def test_experiment_open_includes_rejection_preview_without_full_report():
    reason = "The verifier accepts an empty solution."
    session, response = _open(
        _task(
            1,
            verdict_label="reject",
            verdict_is_good="false",
            verdict_primary_issue=reason,
        )
    )
    verdict = response.model_dump()["tasks"][0]["verdict"]
    assert verdict == {
        "verdict": "reject",
        "is_good": False,
        "confidence": "high",
        "primary_issue": reason,
    }
    sql = _sql(session.calls[2])
    assert (
        "left(coalesce(nullif(tasks.verdict ->> 'primary_issue', ''), tasks.verdict ->> 'reasoning'), 240) AS verdict_primary_issue"
        in sql
    )
    assert len(session.calls) == 4


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


def test_experiment_open_keeps_polling_while_qa_is_running():
    _, response = _open(
        _task(1),
        summary=_summary(has_active_trials=False, qa_running=1),
    )

    assert response.has_active_trials is True
    assert response.summary is not None
    assert response.summary.qa_running == 1


def test_experiment_open_rejects_one_task_shell_over_the_byte_budget():
    oversized = _task(1, task_path="x" * OPEN_MAX_BYTES)
    session = _Session(
        _Result([_identity()]),
        _Result([{"task_id": oversized["task_id"], "created_at": NOW}]),
        _Result([oversized]),
        _Result([_summary(task_count=1)]),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_experiment_open_core(
                session, experiment_id="experiment-1", org_id="org-1"
            )
        )

    assert exc.value.status_code == 413
    assert len(session.calls) == 4


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
    identity_page_sql = _sql(session.calls[1])
    page_sql = _sql(session.calls[2])
    summary_sql = _sql(session.calls[3])

    assert "experiment_task_stats" not in identity_page_sql
    assert "FROM trials" not in identity_page_sql

    for sql in (summary_sql, page_sql):
        # Membership is a FROM clause: the experiment's homed rows and its
        # gathered rows are two index-seekable branches of one UNION ALL, and
        # the visibility rules filter that alias. The whole ``trials`` table
        # is never filtered with ``experiment_id = X OR id IN (gathered)``.
        assert "FROM trials \nWHERE trials.experiment_id = 'experiment-1'" in sql
        assert "JOIN experiment_trials ON experiment_trials.trial_id = trials.id" in sql
        assert "UNION ALL" in sql
        assert "trials.experiment_id = 'experiment-1' OR" not in sql
        assert ".kind = 'agent'" in sql
        assert ".is_probe IS false" in sql
        assert ".superseded_by_trial_id IS NULL" in sql
        assert ".deleted_at IS NULL" in sql
        assert "first_value(" in sql
        assert "OVER (PARTITION BY" in sql
        assert "task_versions.version DESC" in sql
    # The page reads task shells plus aggregates; no trial column, heavy or
    # otherwise, reaches its select list.
    page_select_list = page_sql.split("\nFROM", 1)[0]
    assert "trials." not in page_select_list


def test_public_experiment_open_never_queries_or_serializes_task_owners(monkeypatch):
    task = _task(
        1,
        user="private-owner",
        verdict_primary_issue="Private QA finding",
        tags={
            "github_meta": (
                '{"category":"JS","world":"World_7",'
                '"github_username":"private-owner",'
                '"repository":"private/repository","pr_url":"https://secret"}'
            )
        },
    )
    experiment = SimpleNamespace(
        id="experiment-1",
        name="Public experiment",
        org_id="org-1",
        created_at=NOW,
        updated_at=NOW,
        last_activity_at=NOW + timedelta(minutes=1),
    )

    async def public_experiment(_session, _token):
        return experiment

    monkeypatch.setattr(
        "oddish.core.endpoints.experiment_page.get_public_experiment",
        public_experiment,
    )
    session = _Session(
        _Result([{"task_id": task["task_id"], "created_at": task["created_at"]}]),
        _Result([task]),
    )

    response = asyncio.run(
        get_public_experiment_open_core(
            session,
            public_token="public-token",
            include_summary=False,
        )
    )

    payload = response.model_dump()
    assert "owner" not in payload
    assert "link" not in payload
    assert "user" not in payload["tasks"][0]
    assert payload["tasks"][0]["github_meta"] == {
        "category": "JS",
        "world": "World_7",
    }
    assert "Private QA finding" not in response.model_dump_json()
    assert "primary_issue" not in payload["tasks"][0]["verdict"]
    assert "private-owner" not in response.model_dump_json()
    assert "private/repository" not in response.model_dump_json()
    task_query_sql = _sql(session.calls[1])
    assert 'tasks."user"' not in task_query_sql


def test_later_experiment_page_skips_summary_and_bounds_trial_aggregation():
    task = _task(101)
    page_id = {"task_id": task["task_id"], "created_at": task["created_at"]}
    session = _Session(
        _Result([_identity()]),
        _Result([page_id]),
        _Result([task]),
    )

    response = asyncio.run(
        get_experiment_open_core(
            session,
            experiment_id="experiment-1",
            org_id="org-1",
            before_created_at=NOW,
            before_task_id="task-100",
            include_summary=False,
        )
    )

    assert len(session.calls) == 3
    assert response.summary is None
    assert response.has_active_trials is False
    page_sql = _sql(session.calls[2])
    assert "tasks.id IN ('task-101')" in page_sql
    # Trial columns read from the membership alias, not the ``trials`` table.
    assert ".task_id IN ('task-101')" in page_sql


def test_experiment_focus_resolves_a_task_outside_the_loaded_page():
    task = _task(101)
    session = _Session(
        _Result([_identity()]),
        _Result([task["task_id"]]),
        _Result([task]),
    )

    response = asyncio.run(
        get_experiment_focus_core(
            session,
            experiment_id="experiment-1",
            org_id="org-1",
            task_selector="Task 101",
        )
    )

    assert response.task.id == "task-101"
    assert response.trial is None
    lookup_sql = _sql(session.calls[1])
    assert "task_experiments" in lookup_sql
    assert "tasks.name = 'Task 101'" in lookup_sql


def test_experiment_focus_uses_the_trial_as_host_task_source(monkeypatch):
    async def no_exclusions(_session):
        return CostExclusions()

    monkeypatch.setattr(
        "oddish.core.endpoints.experiment_page.load_cost_exclusions", no_exclusions
    )
    trial = _trial(9)
    trial_row = _trial_page_row(trial)
    session = _Session(
        _Result([_identity()]),
        _Result([trial_row]),
        _Result([_task(1)]),
    )

    response = asyncio.run(
        get_experiment_focus_core(
            session,
            experiment_id="experiment-1",
            org_id="org-1",
            task_selector="stale-task-id",
            trial_id=trial.id,
        )
    )

    assert response.task.id == trial.task_id
    assert response.trial is not None
    assert response.trial.id == trial.id
    trial_sql = _sql(session.calls[1])
    assert ".id = 'trial-009'" in trial_sql
    assert "tasks.id = 'stale-task-id'" not in trial_sql
    assert "experiment_trials" in trial_sql
    assert ".kind = 'agent'" not in trial_sql
    assert ".is_probe IS false" not in trial_sql
    assert ".superseded_by_trial_id IS NULL" not in trial_sql
    assert "effective_task_version_id" not in trial_sql


def test_public_experiment_focus_keeps_grid_trial_visibility():
    trial = _trial(9)
    trial_row = _trial_page_row(trial)
    session = _Session(
        _Result([trial_row]),
        _Result([_task(1)]),
    )

    response = asyncio.run(
        get_experiment_focus_core(
            session,
            experiment_id="experiment-1",
            org_id="org-1",
            trial_id=trial.id,
            _experiment=_identity(),
            _include_cost_exclusion_labels=False,
            _require_grid_trial_visibility=True,
            _public=True,
        )
    )

    assert response.trial is not None
    assert response.trial.id == trial.id
    assert "user" not in response.task.model_dump()
    trial_sql = _sql(session.calls[0])
    assert ".kind = 'agent'" in trial_sql
    assert ".is_probe IS false" in trial_sql
    assert ".superseded_by_trial_id IS NULL" in trial_sql
    assert "effective_task_version_id" in trial_sql
    task_sql = _sql(session.calls[1])
    assert 'tasks."user"' not in task_sql


def test_effective_version_selectable_filters_before_ranking():
    sql = _sql(
        experiment_effective_versions_selectable(
            experiment_id="experiment-1", task_ids=["task-1", "task-2"]
        )
    )
    assert ".task_id IN ('task-1', 'task-2')" in sql
    assert ".task_version_id = tasks.current_version_id" in sql
    assert "task_versions.version DESC" in sql
    assert "effective_task_version_id IS NOT NULL" in sql


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
    assert ".kind = 'agent'" in sql
    assert ".is_probe IS false" in sql
    assert ".superseded_by_trial_id IS NULL" in sql
    # The membership subquery carries every trial column; only the select
    # list decides what reaches the response, so check that.
    select_list = sql.split("\nFROM", 1)[0]
    for heavy_column in (
        ".result AS",
        ".phase_timing AS",
        ".harbor_config AS",
        ".error_message AS",
    ):
        assert heavy_column not in select_list
    assert ".analysis ->> 'classification', 100)" in select_list


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
