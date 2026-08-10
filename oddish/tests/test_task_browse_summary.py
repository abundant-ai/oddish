from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oddish.core.task_browse_projection import (
    build_task_version_browse_projection,
    merge_task_version_browse_summaries,
)
from oddish.core.task_browse_summary import (
    BROWSE_TRIAL_PREVIEW_LIMIT,
    build_task_version_browse_summary,
)


def _row(index: int, **overrides):
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)
    row = {
        "id": f"trial-{index}",
        "name": f"trial-{index}",
        "status": "SUCCESS",
        "reward": 1.0,
        "error_message": None,
        "agent": "codex",
        "model": "unknown-unpriced-model",
        "cost_usd": 0.25,
        "input_tokens": None,
        "output_tokens": None,
        "cache_tokens": None,
        "cache_write_tokens": None,
        "billed_user_id": "user-1",
        "started_at": created_at,
        "finished_at": created_at,
        "created_at": created_at,
    }
    row.update(overrides)
    return row


def test_summary_keeps_exact_totals_with_bounded_trial_previews() -> None:
    total = BROWSE_TRIAL_PREVIEW_LIMIT + 5
    rows = [_row(index) for index in range(total)]

    last_run_at, summary = build_task_version_browse_summary(rows)

    assert summary["total_trials"] == total
    assert summary["completed_trials"] == total
    assert summary["reward_success"] == total
    assert summary["cost_usd"] == total * 0.25
    assert summary["billed_trial_count"] == total
    assert summary["trial_status_counts"]["pass"] == total
    assert summary["trial_groups"] == [
        {
            "agent": "codex",
            "model": "unknown-unpriced-model",
            "trial_count": total,
            "reward_sum": float(total),
            "reward_total": total,
        }
    ]
    assert len(summary["latest_trials"]) == BROWSE_TRIAL_PREVIEW_LIMIT
    assert summary["latest_trials"][0]["id"] == "trial-5"
    assert summary["latest_trials"][-1]["id"] == f"trial-{total - 1}"
    assert last_run_at == rows[-1]["finished_at"]


def test_summary_matches_card_status_buckets() -> None:
    rows = [
        _row(0, status="FAILED", reward=None, error_message="boom"),
        _row(1, status="FAILED", reward=0.5, error_message="AgentTimeoutError"),
        _row(2, status="SUCCESS", reward=None),
        _row(3, status="SKIPPED", reward=None, error_message="baseline failed"),
        _row(4, status="RETRYING", reward=None),
    ]

    _, summary = build_task_version_browse_summary(rows)

    assert summary["failed_trials"] == 2
    assert summary["trial_status_counts"] == {
        "pass": 0,
        "partial": 1,
        "fail": 0,
        "harness-error": 1,
        "scoreless": 1,
        "skipped": 1,
        "pending": 1,
        "queued": 0,
        "running": 0,
    }


def test_projection_merges_page_scoped_live_trials() -> None:
    settled = _row(0, status="SUCCESS", cost_usd=0.25)
    queued = _row(
        1,
        status="QUEUED",
        reward=None,
        cost_usd=0.25,
        finished_at=None,
    )
    running = _row(
        2,
        status="RUNNING",
        reward=None,
        cost_usd=0.5,
        finished_at=None,
    )

    last_run_at, persisted = build_task_version_browse_projection(
        [settled, queued, running]
    )
    _, live = build_task_version_browse_summary([running])
    merged = merge_task_version_browse_summaries(persisted, live)

    assert last_run_at == running["started_at"]
    assert persisted["total_trials"] == 2
    assert persisted["trial_status_counts"]["queued"] == 1
    assert merged["total_trials"] == 3
    assert merged["completed_trials"] == 1
    assert merged["cost_usd"] == 1.0
    assert merged["trial_status_counts"]["pass"] == 1
    assert merged["trial_status_counts"]["running"] == 1
    assert [trial["id"] for trial in merged["latest_trials"]] == [
        "trial-0",
        "trial-1",
        "trial-2",
    ]
