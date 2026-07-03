"""Unit tests for bulk-retry trial selection (skipped exclusion)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.retry import _failed_trial_ids  # noqa: E402


def _task():
    return {
        "trials": [
            {"id": "t-1", "status": "failed"},
            {"id": "t-2", "status": "skipped"},
            {"id": "t-3", "status": "success"},
            {"id": "t-4", "status": "skipped", "superseded_by_trial_id": "t-9"},
            {"id": "t-5", "status": "queued"},
        ]
    }


def test_bulk_retry_excludes_skipped_by_default():
    # Default bulk retry sweeps only FAILED; gate-skipped trials are left alone.
    assert _failed_trial_ids(_task()) == ["t-1"]


def test_bulk_retry_includes_skipped_when_opted_out():
    # --no-baseline-gate -> include_skipped: FAILED + live SKIPPED (not the
    # superseded one).
    assert _failed_trial_ids(_task(), include_skipped=True) == ["t-1", "t-2"]
