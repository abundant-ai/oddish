"""SKIPPED trials are terminal, so they count toward task "done".

`total` includes skipped (a non-pass in the denominator), but skipped is passed
to resolve_task_status so `completed + failed + skipped >= total` can still flip
the task to COMPLETED — otherwise a gate-skipped task would hang non-terminal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.helpers import resolve_task_status
from oddish.db import TaskStatus


def _task():
    return SimpleNamespace(status=TaskStatus.RUNNING)


def test_skipped_trials_count_toward_done():
    # 2 succeeded + 3 skipped = 5 terminal of 5 total -> COMPLETED.
    assert (
        resolve_task_status(_task(), total=5, completed=2, failed=0, skipped=3)
        == TaskStatus.COMPLETED
    )


def test_without_skipped_the_same_task_would_hang():
    # Regression guard: if skipped weren't counted, 2 + 0 < 5 -> never COMPLETED.
    assert (
        resolve_task_status(_task(), total=5, completed=2, failed=0)
        == TaskStatus.RUNNING
    )


def test_still_running_when_a_trial_is_pending():
    # 2 done + 2 skipped, 1 still pending -> 4 < 5 -> not COMPLETED.
    assert (
        resolve_task_status(_task(), total=5, completed=2, failed=0, skipped=2)
        == TaskStatus.RUNNING
    )
