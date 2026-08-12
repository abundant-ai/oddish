from __future__ import annotations

import asyncio
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.task_detail import _experiments_by_version
from oddish.core.endpoints.task_open import get_task_open_core

NOW = datetime(2026, 8, 11, tzinfo=UTC)


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


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def execute(self, query, params=None):
        self.calls.append((query, params))
        if not self.results:
            raise AssertionError("unexpected query")
        return self.results.pop(0)


def _identity(
    *,
    selected=True,
    selected_version_id="task-1-v2",
    selected_version=2,
    status="running",
):
    verdict = {
        "is_good": False,
        "confidence": "high",
        "primary_issue": "Verifier accepts an empty result",
        "reasoning": "The task needs a stronger assertion.",
        "recommendations": ["Assert the generated output."],
        "trial_classifications": [{"result": "x" * 20_000}],
    }
    return {
        "task_id": "task-1",
        "name": "bounded-task",
        "status": status,
        "priority": "low",
        "user": "octocat",
        "task_path": "/tasks/bounded-task",
        "link": "https://github.com/acme/repo/pull/1",
        "tags": {"github_username": "octocat"},
        "run_analysis": True,
        "verdict_status": "failed",
        "verdict": verdict,
        "verdict_error": "judge timed out",
        "current_version_id": "task-1-v2",
        "created_at": NOW,
        "updated_at": NOW,
        "default_version_id": "task-1-v2",
        "default_version": 2,
        "default_version_message": "current",
        "default_version_created_at": NOW,
        "selected_version_id": selected_version_id if selected else None,
        "selected_version": selected_version if selected else None,
        "selected_version_message": "selected" if selected else None,
        "selected_version_created_at": NOW if selected else None,
        "experiments": [{"id": "exp-all-time", "name": "All time"}],
        "task_tags": [],
        "selected_version_tags": [
            {
                "tag_id": "tag-selected",
                "key": "selected",
                "value": None,
                "color": "#123456",
                "visibility": "PRIVATE",
                "current": True,
                "older": False,
            }
        ],
    }


def _group(
    *,
    selected,
    current,
    total,
    completed=None,
    failed=0,
    skipped=0,
    billed=False,
    native_cost=0.0,
    agent="codex",
    model="openai/gpt-5.6",
    provider="openai",
    is_probe=False,
    duration_sum_seconds=0.0,
    duration_trial_count=0,
):
    completed = total - failed - skipped if completed is None else completed
    return {
        "is_selected": selected,
        "is_current": current,
        "is_billed": billed,
        "is_probe": is_probe,
        "agent": agent,
        "provider": provider,
        "model": model,
        "total": total,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "pass_count": completed,
        "partial_count": 0,
        "fail_count": 0,
        "reward_sum": float(completed),
        "reward_total": completed,
        "last_run_at": NOW,
        "duration_sum_seconds": duration_sum_seconds,
        "duration_trial_count": duration_trial_count,
        "token_count": total * 100,
        "token_trial_count": total,
        "native_cost": native_cost,
        "native_count": total,
        "estimated_count": 0,
        "estimated_input": 0,
        "estimated_output": 0,
        "estimated_cache": 0,
        "estimated_cache_write": 0,
    }


def _preview(index):
    return {
        "id": f"trial-{index:03}",
        "name": f"trial-{index:03}",
        "experiment_id": "exp-current",
        "task_version_id": "task-1-v2",
        "agent": "codex",
        "provider": "openai",
        "model": "openai/gpt-5.6",
        "status": "success",
        "reward": 1.0,
        "error_kind": None,
        "is_probe": False,
        "cost_usd": 0.01,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_tokens": 0,
        "cache_write_tokens": 0,
        "billed_user_id": "user-1",
        "created_at": NOW,
        "started_at": NOW,
        "finished_at": NOW,
        # Deliberately heavy fields that the mapper must never carry.
        "result": {"huge": "x" * 20_000},
        "analysis": {"huge": "x" * 20_000},
        "error_message": "x" * 20_000,
        "jobs": [{"id": "job-1"}],
    }


def _aggregate(groups, experiments=None, qa_cost=1.25):
    return {
        "groups": groups,
        "experiments": experiments or [],
        "qa_cost_usd": qa_cost,
    }


def _open(identity, aggregate, previews=None, *, version_id=None):
    session = _Session(
        _Result([identity]),
        _Result([aggregate]),
        _Result(previews or []),
    )
    response = asyncio.run(
        get_task_open_core(
            session,
            task_id="task-1",
            version_id=version_id,
            org_id="org-1",
        )
    )
    return session, response


def test_task_open_is_org_scoped_exact_compact_and_bounded():
    groups = [
        _group(
            selected=True,
            current=True,
            billed=True,
            total=500,
            native_cost=50.0,
        ),
        # This selected-version cohort is older than the newest 20 preview rows.
        _group(
            selected=True,
            current=True,
            total=1,
            native_cost=0.2,
            agent="legacy-agent",
            model="legacy/model",
            provider="legacy",
        ),
        _group(selected=False, current=False, total=700, native_cost=70.0),
    ]
    session, response = _open(
        _identity(),
        _aggregate(groups, [{"id": "exp-current", "name": "Current"}]),
        [_preview(index) for index in range(21)],
    )

    assert len(session.calls) == 3
    assert all(call[1]["org_id"] == "org-1" for call in session.calls)
    assert all("org_id" in str(call[0]) for call in session.calls)
    assert session.calls[1][1]["current_version_id"] == "task-1-v2"
    assert response.task.status == "completed"
    assert response.task.run_analysis is True
    assert response.task.verdict_status == "failed"
    assert response.task.verdict is not None
    assert response.task.verdict.is_good is False
    assert response.task.verdict.primary_issue == "Verifier accepts an empty result"
    assert response.task.verdict_error == "judge timed out"

    selected = response.selected_version
    assert selected is not None
    assert selected.trial_count == 501
    assert [(item.id, item.name) for item in selected.experiments] == [
        ("exp-current", "Current")
    ]
    assert [(item.tag_id, item.key) for item in selected.user_tags] == [
        ("tag-selected", "selected")
    ]
    assert {group.agent for group in selected.agent_models} == {
        "codex",
        "legacy-agent",
    }
    legacy = next(
        group for group in selected.agent_models if group.agent == "legacy-agent"
    )
    assert legacy.trial_count == 1
    assert legacy.reward_sum == pytest.approx(1.0)
    assert legacy.cost_usd == pytest.approx(0.2)
    assert all(trial.agent == "codex" for trial in response.trials)

    assert response.totals.total_trials == 1201
    assert response.totals.cost_usd == pytest.approx(120.2)
    assert response.totals.billed_cost_usd == pytest.approx(50.0)
    assert response.totals.qa_cost_usd == pytest.approx(1.25)
    assert len(response.trials) == 20
    assert response.trials_has_more is True

    payload = response.model_dump_json()
    assert len(payload.encode()) < 50_000
    for excluded in (
        '"result"',
        '"analysis"',
        '"jobs"',
        '"error_message"',
        '"trial_classifications"',
    ):
        assert excluded not in payload


def test_task_open_agent_duration_is_exact_across_folded_rows():
    _, response = _open(
        _identity(),
        _aggregate(
            [
                _group(
                    selected=True,
                    current=True,
                    total=2,
                    provider="openai",
                    duration_sum_seconds=30.0,
                    duration_trial_count=2,
                ),
                _group(
                    selected=True,
                    current=True,
                    total=1,
                    billed=True,
                    provider="azure",
                    duration_sum_seconds=45.0,
                    duration_trial_count=1,
                ),
            ]
        ),
        [_preview(1)],
    )

    assert response.selected_version is not None
    assert len(response.selected_version.agent_models) == 1
    summary = response.selected_version.agent_models[0]
    assert summary.providers == ["azure", "openai"]
    assert summary.duration_sum_seconds == pytest.approx(75.0)
    assert summary.duration_trial_count == 3
    assert summary.duration_sum_seconds / summary.duration_trial_count == pytest.approx(
        25.0
    )


def test_task_open_json_group_timestamps_serialize_as_datetimes():
    row = _group(selected=True, current=True, total=1)
    row["last_run_at"] = NOW.isoformat()
    _, response = _open(
        _identity(),
        _aggregate([row]),
        [_preview(1)],
    )

    assert response.selected_version is not None
    assert response.selected_version.last_run_at == NOW
    assert response.selected_version.last_run_at.tzinfo is not None
    assert response.selected_version.agent_models[0].last_run_at == NOW
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        payload = response.model_dump_json()
    assert '"last_run_at":"2026-08-11T00:00:00Z"' in payload
    assert not any(
        "PydanticSerializationUnexpectedValue" in str(item.message) for item in emitted
    )


def test_task_open_historical_version_experiments_match_detail_population():
    identity = _identity(
        selected_version_id="task-1-v1",
        selected_version=1,
    )
    experiments = [
        {"id": "exp-a", "name": "Alpha"},
        {"id": "exp-b", "name": "Beta"},
    ]
    _, response = _open(
        identity,
        _aggregate(
            [
                _group(selected=True, current=False, total=3),
                _group(selected=False, current=True, total=1, completed=0),
            ],
            experiments,
        ),
        [_preview(1)],
        version_id="task-1-v1",
    )

    detail_population = _experiments_by_version(
        [
            SimpleNamespace(
                task_version_id="task-1-v1", experiment_id="exp-b", is_probe=False
            ),
            SimpleNamespace(
                task_version_id="task-1-v1", experiment_id="exp-a", is_probe=False
            ),
            SimpleNamespace(
                task_version_id="task-1-v1", experiment_id="exp-a", is_probe=True
            ),
        ],
        {"exp-a": "Alpha", "exp-b": "Beta"},
    )["task-1-v1"]
    assert response.selected_version is not None
    assert response.selected_version.is_current is False
    assert response.selected_version.experiments == detail_population
    assert response.task.experiments[0].id == "exp-all-time"


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        (
            [
                _group(selected=True, current=False, total=2),
                _group(selected=False, current=True, total=2, completed=1),
            ],
            "running",
        ),
        (
            [
                _group(selected=True, current=False, total=2, completed=1),
                _group(selected=False, current=True, total=2),
            ],
            "completed",
        ),
    ],
    ids=["historical-terminal-current-active", "current-terminal-historical-active"],
)
def test_task_open_status_is_always_current_version_scoped(groups, expected):
    _, response = _open(
        _identity(selected_version_id="task-1-v1", selected_version=1),
        _aggregate(groups),
        [_preview(1)],
        version_id="task-1-v1",
    )
    assert response.task.status == expected


def test_task_open_legacy_status_uses_all_live_trials() -> None:
    identity = _identity(selected=False, status="running")
    identity.update(
        current_version_id=None,
        default_version_id=None,
        default_version=None,
        default_version_message=None,
        default_version_created_at=None,
    )
    session, response = _open(
        identity,
        _aggregate([_group(selected=False, current=True, total=2)]),
    )

    assert response.task.status == "completed"
    assert response.default_version is None
    assert response.selected_version is None
    assert len(session.calls) == 2


def test_task_open_current_status_ignores_probe_rows_but_keeps_probe_rollups():
    _, response = _open(
        _identity(),
        _aggregate(
            [
                _group(selected=True, current=True, total=1),
                _group(
                    selected=True,
                    current=True,
                    total=1,
                    completed=0,
                    agent="probe-agent",
                    is_probe=True,
                ),
            ]
        ),
        [_preview(1)],
    )

    assert response.task.status == "completed"
    assert response.totals.total_trials == 2
    assert response.selected_version is not None
    assert response.selected_version.trial_count == 2
    probe = next(
        group for group in response.selected_version.agent_models if group.is_probe
    )
    assert probe.agent == "probe"
    assert probe.trial_count == 1
    assert probe.pending_count == 1


def test_task_open_queries_encode_detail_eligible_population():
    session, _ = _open(
        _identity(),
        _aggregate([_group(selected=True, current=True, total=1)]),
        [_preview(1)],
    )
    aggregate_sql = str(session.calls[1][0])
    preview_sql = str(session.calls[2][0])
    for sql in (aggregate_sql, preview_sql):
        assert "deleted_at IS NULL" in sql
        assert "superseded_by_trial_id IS NULL" in sql
        assert "NOT LIKE 'combine:%'" in sql
    assert "tr.is_probe IS NOT TRUE" in aggregate_sql
    assert "tr.task_version_id = CAST(:version_id AS text)" in aggregate_sql
    assert "JOIN experiments e ON e.id = tr.experiment_id" in aggregate_sql
    assert "SELECT tr.*" not in aggregate_sql
    for heavy_column in (
        "tr.result",
        "tr.analysis",
        "tr.trajectory_summary",
        "tr.error_message",
    ):
        assert heavy_column not in aggregate_sql
    assert "AS duration_sum_seconds" in aggregate_sql
    assert "AS duration_trial_count" in aggregate_sql
    assert "tr.finished_at >= tr.started_at" in aggregate_sql
    assert "LIMIT 21" in preview_sql
    identity_sql = str(session.calls[0][0])
    assert "assignment.target_id = i.selected_version_id" in identity_sql
    assert "assignment.scope = 'VERSION'" in identity_sql
    assert "AS selected_version_tags" in identity_sql


def test_task_open_missing_task_stops_after_identity():
    session = _Session(_Result([]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_task_open_core(session, task_id="missing", org_id="org-1"))
    assert exc.value.status_code == 404
    assert len(session.calls) == 1


def test_task_open_rejects_version_outside_selected_task():
    session = _Session(_Result([_identity(selected=False)]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_task_open_core(
                session,
                task_id="task-1",
                version_id="other-task-v1",
                org_id="org-1",
            )
        )
    assert exc.value.status_code == 404
    assert len(session.calls) == 1
