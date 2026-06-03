"""Tests for merge_resubmission_tags.

When the same task is re-run from a different PR, the task row is reused and
must pick up the new PR's ``github_meta`` so PR-comment refreshes target the
PR the run came from — not the PR that first created the task.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.helpers import merge_resubmission_tags  # noqa: E402


def test_incoming_github_meta_overrides_existing():
    existing = {"github_meta": {"pr_number": "390", "pr_repo": "abundant-ai/experiments"}}
    incoming = {"github_meta": {"pr_number": "391", "pr_repo": "abundant-ai/experiments"}}
    merged = merge_resubmission_tags(existing, incoming)
    assert merged["github_meta"]["pr_number"] == "391"


def test_existing_only_tags_are_preserved():
    existing = {"github_meta": {"pr_number": "390"}, "kept": "yes"}
    incoming = {"github_meta": {"pr_number": "391"}}
    merged = merge_resubmission_tags(existing, incoming)
    assert merged["kept"] == "yes"
    assert merged["github_meta"]["pr_number"] == "391"


def test_none_incoming_value_does_not_clear_existing():
    existing = {"github_meta": {"pr_number": "390"}}
    incoming = {"github_meta": None}
    merged = merge_resubmission_tags(existing, incoming)
    assert merged["github_meta"]["pr_number"] == "390"


def test_empty_and_none_inputs():
    assert merge_resubmission_tags(None, None) == {}
    assert merge_resubmission_tags(None, {"a": 1}) == {"a": 1}
    assert merge_resubmission_tags({"a": 1}, None) == {"a": 1}
    assert merge_resubmission_tags({"a": 1}, {}) == {"a": 1}


def test_returns_new_dict_not_existing_reference():
    # The append path reassigns task.tags to mark the JSON column dirty, so the
    # helper must not return the same object it was given.
    existing = {"github_meta": {"pr_number": "390"}}
    merged = merge_resubmission_tags(existing, {"x": 1})
    assert merged is not existing
