"""Combine copies must not inflate task-level trial counts.

``combine_experiments_core`` materializes a source trial as a new row under the
*same* task. Copies are peers of the original, not replacements, so the
``superseded_by_trial_id`` filter does not hide them and the task page's count
compounds once per consolidation. These tests pin the exclusion, and pin that it
stays opt-in so experiment-scoped views (where every row of a *combined*
experiment is a copy) don't render empty.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.cost_basis import is_combine_copy
from oddish.core.endpoints.deletion import _combine_idempotency_key
from oddish.core.helpers import get_task_status_trials


class _Trial:
    def __init__(self, trial_id, *, idempotency_key=None, superseded_by=None):
        self.id = trial_id
        self.idempotency_key = idempotency_key
        self.superseded_by_trial_id = superseded_by
        self.task_version_id = "v1"


class _Task:
    def __init__(self, trials):
        self.trials = trials
        self.current_version_id = "v1"


def test_is_combine_copy_matches_only_marked_rows():
    assert is_combine_copy(_Trial("t", idempotency_key="combine:r:s"))
    assert not is_combine_copy(_Trial("t", idempotency_key="retry-dedupe-key"))
    assert not is_combine_copy(_Trial("t", idempotency_key=None))


def test_is_combine_copy_matches_the_hashed_long_id_form():
    """Long ids hash past the readable form; the prefix must survive."""
    key = _combine_idempotency_key("result-" + "r" * 80, "source-" + "s" * 80)
    assert is_combine_copy(_Trial("t", idempotency_key=key))


def _task_with_five_real_and_two_generations_of_copies():
    real = [_Trial(f"real-{i}") for i in range(5)]
    gen1 = [
        _Trial(f"c1-{i}", idempotency_key=_combine_idempotency_key("exp-1", t.id))
        for i, t in enumerate(real)
    ]
    gen2 = [
        _Trial(f"c2-{i}", idempotency_key=_combine_idempotency_key("exp-2", t.id))
        for i, t in enumerate(real + gen1)
    ]
    return _Task(real + gen1 + gen2), real


def test_excluded_count_is_real_executions_only():
    task, real = _task_with_five_real_and_two_generations_of_copies()

    kept = get_task_status_trials(task, exclude_combine_copies=True)

    assert [t.id for t in kept] == [t.id for t in real]


def test_copies_compound_when_not_excluded():
    """Guards the regression itself: this is where 5 runs became hundreds.

    Each consolidation copies everything present, copies included, so the
    unfiltered total doubles per generation: 5 -> 10 -> 20.
    """
    task, real = _task_with_five_real_and_two_generations_of_copies()

    assert len(get_task_status_trials(task)) == 20
    assert len(get_task_status_trials(task, exclude_combine_copies=True)) == len(real)


def test_exclusion_is_off_by_default_so_experiment_views_keep_their_rows():
    """A combined experiment's trials are *all* copies; defaulting on empties it."""
    copies = [
        _Trial(f"c-{i}", idempotency_key=_combine_idempotency_key("exp-1", f"src-{i}"))
        for i in range(3)
    ]
    task = _Task(copies)

    assert len(get_task_status_trials(task)) == 3
    assert get_task_status_trials(task, exclude_combine_copies=True) == []


def test_superseded_rows_stay_filtered_in_both_modes():
    task = _Task(
        [
            _Trial("live"),
            _Trial("retried", superseded_by="live"),
            _Trial(
                "copy-superseded",
                idempotency_key=_combine_idempotency_key("exp-1", "retried"),
                superseded_by="live",
            ),
        ]
    )

    assert [t.id for t in get_task_status_trials(task)] == ["live"]
    assert [
        t.id for t in get_task_status_trials(task, exclude_combine_copies=True)
    ] == ["live"]
