"""SKIPPED trials in the GitHub PR progress line.

Under the "skipped ≡ non-pass, but terminal" rule:
  * skipped counts as DONE for trial progress (so the comment advances past
    "🔄 Progress" and the bar can reach 100%), and stays in the total;
  * skipped is NOT analyzable (never ran, no trajectory), so the analysis
    stages use a skipped-excluding denominator (``analyzable_trials``).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

formatter = importlib.import_module("oddish.integrations.github.formatter")
TrialSummary = formatter.TrialSummary
TaskSummary = formatter.TaskSummary


def _trial(index: int, status: str, *, reward: float | None = None) -> TrialSummary:
    return TrialSummary(
        index=index,
        trial_id=f"task-1-{index}",
        agent="claude-code",
        model="fireworks/kimi-k2p7-code",
        status=status,
        reward=reward,
        duration_seconds=None,
        analysis_status=None,
        classification=None,
    )


def _task(trials: list[TrialSummary]) -> TaskSummary:
    return TaskSummary(
        task_id="task-1",
        task_name="task-1",
        task_url="https://example/task-1",
        trials=trials,
        verdict_status=None,
        verdict=None,
    )


def _comment(task: TaskSummary) -> str:
    return formatter.format_experiment_comment(
        [task], "exp-1", "https://example/exp-1"
    )


def test_all_trials_terminal_advances_to_analyzing_with_skipped_excluded_denominator():
    # 1 pass + 1 fail + 3 skipped. Every trial is terminal, so we advance past
    # "🔄 Progress"; only the 2 non-skipped trials are analyzable.
    comment = _comment(
        _task(
            [
                _trial(0, "success", reward=1.0),
                _trial(1, "failed"),
                _trial(2, "skipped"),
                _trial(3, "skipped"),
                _trial(4, "skipped"),
            ]
        )
    )
    assert "🔄 Progress" not in comment
    assert "Analyzing results" in comment
    # analysis denominator excludes the 3 skipped -> 0/2, not 0/5.
    assert "0/2 classified" in comment


def test_skipped_count_as_done_but_stay_in_total_while_a_trial_runs():
    # 1 pass + 1 running + 2 skipped. Done = pass + 2 skipped = 3 of 4 total.
    comment = _comment(
        _task(
            [
                _trial(0, "success", reward=1.0),
                _trial(1, "running"),
                _trial(2, "skipped"),
                _trial(3, "skipped"),
            ]
        )
    )
    assert "3/4 trials complete" in comment


def _task_comment(task: TaskSummary) -> str:
    return formatter.format_task_comment(task, "exp-1", "https://example/exp-1")


def test_single_task_comment_advances_past_running_with_skipped():
    # format_task_comment (single-task path) must also count skipped as done and
    # exclude it from the analysis denominator, not stay stuck on "🔄 Running".
    comment = _task_comment(
        _task(
            [
                _trial(0, "success", reward=1.0),
                _trial(1, "failed"),
                _trial(2, "skipped"),
                _trial(3, "skipped"),
            ]
        )
    )
    assert "🔄 Running" not in comment
    assert "Analyzing Results" in comment
    assert "0/2 classified" in comment  # analyzable = 4 total - 2 skipped


def test_single_task_all_skipped_not_stuck_running():
    comment = _task_comment(_task([_trial(0, "skipped"), _trial(1, "skipped")]))
    assert "🔄 Running" not in comment
