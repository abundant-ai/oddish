from __future__ import annotations

from datetime import UTC, datetime

from oddish.core.endpoints.task_open_aggregates import fold_task_open_groups
from oddish.schemas import TaskOpenVersionSummary

NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _identity() -> dict[str, object]:
    return {
        "selected_version_id": "task-1-v1",
        "selected_version": 1,
        "selected_version_message": "first",
        "selected_version_created_at": NOW,
        "current_version_id": "task-1-v1",
    }


def _group() -> dict[str, object]:
    return {
        "is_selected": True,
        "is_current": True,
        "is_billed": False,
        "is_probe": False,
        "agent": "codex",
        "provider": "openai",
        "model": "openai/gpt-5.6",
        "total": 2,
        "completed": 1,
        "failed": 0,
        "skipped": 0,
        "pass_count": 1,
        "partial_count": 0,
        "fail_count": 0,
        "reward_sum": 1.0,
        "reward_total": 1,
        "last_run_at": NOW,
        "duration_sum_seconds": 30.0,
        "duration_trial_count": 1,
        "token_count": 100,
        "token_trial_count": 1,
        "native_cost": 0.25,
        "native_count": 1,
        "estimated_count": 0,
        "estimated_input": 0,
        "estimated_output": 0,
        "estimated_cache": 0,
        "estimated_cache_write": 0,
    }


def test_task_open_fold_builds_exact_bounded_rollups() -> None:
    totals, selected, current_counts = fold_task_open_groups(
        [_group()], _identity(), 0.5
    )

    assert selected is not None
    assert selected.trial_count == 2
    assert selected.completed_count == 1
    assert selected.pending_count == 1
    assert selected.cost_usd == 0.25
    assert len(selected.agent_models) == 1
    assert selected.agent_models[0].providers == ["openai"]
    assert totals.total_trials == 2
    assert totals.qa_cost_usd == 0.5
    assert current_counts == (2, 1)


def test_task_open_version_contract_excludes_detail_only_metadata() -> None:
    payload = TaskOpenVersionSummary(
        id="task-1-v1",
        version=1,
        created_at=NOW,
    ).model_dump()

    for detail_only_field in (
        "content_hash",
        "pre_trial_findings",
        "pre_trial_status",
        "pre_trial_error",
        "pre_trial_cost_usd",
    ):
        assert detail_only_field not in payload
