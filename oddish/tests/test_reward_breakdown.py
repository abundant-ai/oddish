"""Tests for the reward-breakdown surfacing (CLI formatter + backfill extractor)."""

from __future__ import annotations

from oddish.backfill_reward_breakdown import extract_breakdown
from oddish.cli.api import format_reward_breakdown

_PARTIAL = {
    "reward_breakdown": {
        "reward": 0.2,
        "code_fraction": 0.0,
        "code_tests_passed": 0,
        "code_tests_total": 12,
        "code_exit": 1,
        "workflow_fraction": 0.6667,
        "workflow_passed": 4,
        "workflow_total": 6,
        "workflow_failures_count": 2,
    }
}


def test_format_reward_breakdown_partial() -> None:
    assert format_reward_breakdown(_PARTIAL) == "code 0/12 · workflow 4/6"


def test_format_reward_breakdown_full_pass() -> None:
    result = {
        "reward_breakdown": {
            "reward": 1.0,
            "code_tests_passed": 583,
            "code_tests_total": 583,
            "workflow_passed": 6,
            "workflow_total": 6,
        }
    }
    assert format_reward_breakdown(result) == "code 583/583 · workflow 6/6"


def test_format_reward_breakdown_fraction_fallback() -> None:
    # No per-test totals -> fall back to percentages.
    result = {
        "reward_breakdown": {"code_fraction": 0.5, "workflow_fraction": 1.0}
    }
    assert format_reward_breakdown(result) == "code 50% · workflow 100%"


def test_format_reward_breakdown_absent() -> None:
    assert format_reward_breakdown(None) == ""
    assert format_reward_breakdown({}) == ""
    assert format_reward_breakdown({"reward_breakdown": None}) == ""
    assert format_reward_breakdown("not a dict") == ""


def test_extract_breakdown_keeps_numeric_subrewards() -> None:
    reward_json = {
        "reward": 0.2,
        "code_fraction": 0.0,
        "code_tests_total": 12,
        "workflow_passed": 4,
        "workflow_total": 6,
        # non-numeric / unknown keys are dropped
        "workflow_failures": ["x"],
        "note": "ignore me",
    }
    bd = extract_breakdown(reward_json)
    assert bd == {
        "reward": 0.2,
        "code_fraction": 0.0,
        "code_tests_total": 12,
        "workflow_passed": 4,
        "workflow_total": 6,
    }


def test_extract_breakdown_scalar_only_is_none() -> None:
    # Only a scalar reward (binary grader) -> no real breakdown.
    assert extract_breakdown({"reward": 1.0}) is None
    assert extract_breakdown({}) is None
    assert extract_breakdown(None) is None
    assert extract_breakdown("nope") is None
