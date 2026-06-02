"""Tests for the GitHub PR comment formatter.

These are pure-function tests — no DB, no network, no env. They lock in that
the rendered comment exposes run *performance* (reward + aggregate pass-rate),
not just metadata, for both the single-task and multi-task (experiment) views.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.integrations.github.formatter import (  # noqa: E402
    TaskSummary,
    TrialSummary,
    format_experiment_comment,
    format_task_comment,
)


def _trial(
    index: int,
    *,
    status: str,
    reward: float | None,
    agent: str = "claude-code",
    analysis_status: str | None = "success",
    classification: str | None = "GOOD_SUCCESS",
) -> TrialSummary:
    return TrialSummary(
        index=index,
        trial_id=f"trial-{index}",
        agent=agent,
        model="claude-sonnet-4-5",
        status=status,
        reward=reward,
        duration_seconds=42.0,
        analysis_status=analysis_status,
        classification=classification,
    )


def _task(name: str, trials: list[TrialSummary], **kw) -> TaskSummary:
    return TaskSummary(
        task_id=f"task-{name}",
        task_name=name,
        task_url=f"https://www.oddish.app/experiments/exp-1",
        trials=trials,
        verdict_status=kw.get("verdict_status"),
        verdict=kw.get("verdict"),
    )


# ---------------------------------------------------------------------------
# Single-task comment
# ---------------------------------------------------------------------------


def test_task_comment_shows_reward_column_and_values():
    task = _task(
        "fix-cve",
        [
            _trial(0, status="success", reward=1.0),
            _trial(1, status="failed", reward=0.0),
            _trial(2, status="success", reward=0.5),
        ],
    )
    body = format_task_comment(task, "my-exp", "https://www.oddish.app/experiments/exp-1")

    # Reward column header is present and the values render.
    assert "Reward" in body
    assert "✓" in body  # reward == 1.0
    assert "✗" in body  # reward == 0.0
    assert "~ 0.50" in body  # partial reward


def test_task_comment_performance_line():
    task = _task(
        "fix-cve",
        [
            _trial(0, status="success", reward=1.0),
            _trial(1, status="failed", reward=0.0),
        ],
    )
    body = format_task_comment(task, "my-exp", "https://www.oddish.app/experiments/exp-1")
    assert "**Performance:** 1/2 trials passed (50%)" in body


def test_task_comment_no_performance_line_while_queued():
    task = _task(
        "fix-cve",
        [
            _trial(0, status="queued", reward=None, analysis_status=None, classification=None),
            _trial(1, status="queued", reward=None, analysis_status=None, classification=None),
        ],
    )
    body = format_task_comment(task, "my-exp", "https://www.oddish.app/experiments/exp-1")
    # Nothing has finished yet -> no misleading pass-rate.
    assert "**Performance:**" not in body
    assert "Queued" in body


# ---------------------------------------------------------------------------
# Multi-task / experiment comment (the common /oddish sweep case)
# ---------------------------------------------------------------------------


def test_experiment_comment_has_status_and_reward_columns():
    tasks = [
        _task("task-a", [_trial(0, status="success", reward=1.0)]),
        _task("task-b", [_trial(0, status="failed", reward=0.0, agent="codex")]),
    ]
    body = format_experiment_comment(
        tasks, "my-exp", "https://www.oddish.app/experiments/exp-1"
    )

    # The experiment table previously had no Status/Reward columns at all.
    header = next(line for line in body.splitlines() if line.startswith("| Task |"))
    assert "Status" in header
    assert "Reward" in header

    # And the reward values actually appear in the rendered rows.
    assert "✓" in body
    assert "✗" in body


def test_experiment_comment_aggregate_performance():
    tasks = [
        _task(
            "task-a",
            [
                _trial(0, status="success", reward=1.0),
                _trial(1, status="success", reward=1.0),
            ],
        ),
        _task(
            "task-b",
            [
                _trial(0, status="failed", reward=0.0, agent="codex"),
                _trial(1, status="success", reward=1.0, agent="codex"),
            ],
        ),
    ]
    body = format_experiment_comment(
        tasks, "my-exp", "https://www.oddish.app/experiments/exp-1"
    )
    # 3 of 4 completed trials passed.
    assert "**Performance:** 3/4 trials passed (75%)" in body


def test_experiment_comment_no_performance_line_while_queued():
    tasks = [
        _task(
            "task-a",
            [_trial(0, status="queued", reward=None, analysis_status=None, classification=None)],
        )
    ]
    body = format_experiment_comment(
        tasks, "my-exp", "https://www.oddish.app/experiments/exp-1"
    )
    assert "**Performance:**" not in body
