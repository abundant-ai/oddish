#!/usr/bin/env python3
"""Render sample Oddish PR comments to stdout — no DB/network/env.

Useful for eyeballing the exact markdown the GitHub integration would post.

    uv run python oddish/scripts/preview_comment.py
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


def _trial(i, agent, status, reward, classification):
    return TrialSummary(
        index=i,
        trial_id=f"trial-{i}",
        agent=agent,
        model="claude-sonnet-4-5" if agent == "claude-code" else "gpt-5.2-codex",
        status=status,
        reward=reward,
        duration_seconds=120.0 + i * 30,
        analysis_status="success" if status != "queued" else None,
        classification=classification,
    )


def _task(name, trials, verdict_status=None, verdict=None):
    return TaskSummary(
        task_id=f"task-{name}",
        task_name=name,
        task_url="https://www.oddish.app/experiments/exp-demo",
        trials=trials,
        verdict_status=verdict_status,
        verdict=verdict,
    )


single = _task(
    "fix-cve-2024-1234",
    [
        _trial(0, "claude-code", "success", 1.0, "GOOD_SUCCESS"),
        _trial(1, "claude-code", "failed", 0.0, "GOOD_FAILURE"),
        _trial(2, "claude-code", "success", 0.5, "BAD_SUCCESS"),
    ],
    verdict_status="success",
    verdict={"is_good": True, "success_count": 2, "task_problem_count": 1},
)

experiment = [
    _task(
        "task-a",
        [
            _trial(0, "claude-code", "success", 1.0, "GOOD_SUCCESS"),
            _trial(1, "codex", "failed", 0.0, "GOOD_FAILURE"),
        ],
    ),
    _task(
        "task-b",
        [
            _trial(0, "claude-code", "success", 1.0, "GOOD_SUCCESS"),
            _trial(1, "codex", "success", 1.0, "GOOD_SUCCESS"),
        ],
    ),
]


if __name__ == "__main__":
    print("=" * 80)
    print("SINGLE-TASK COMMENT")
    print("=" * 80)
    print(format_task_comment(single, "demo-exp", "https://www.oddish.app/experiments/exp-demo"))
    print()
    print("=" * 80)
    print("MULTI-TASK / EXPERIMENT COMMENT")
    print("=" * 80)
    print(
        format_experiment_comment(
            experiment, "demo-exp", "https://www.oddish.app/experiments/exp-demo"
        )
    )
