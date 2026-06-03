from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.dashboard import _experiment_row_passes_status_filter


def _row(**overrides):
    base = {
        "active_trials": 0,
        "retrying_trials": 0,
        "verdict_needs_review": 0,
        "verdict_pending": 0,
        "verdict_failed": 0,
        "failed_trials": 0,
    }
    base.update(overrides)
    return base


def test_retrying_experiment_status_filter_matches_retrying_trials() -> None:
    assert _experiment_row_passes_status_filter(
        _row(active_trials=3, retrying_trials=1),
        status_filter="retrying",
    )


def test_retrying_experiment_status_filter_ignores_other_active_trials() -> None:
    assert not _experiment_row_passes_status_filter(
        _row(active_trials=3, retrying_trials=0),
        status_filter="retrying",
    )
