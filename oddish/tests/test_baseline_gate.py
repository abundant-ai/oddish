"""Unit tests for the pure baseline-gate decision.

``evaluate_baseline_gate`` decides whether a task's nop/oracle baselines
validate it: *every* oracle run must pass (reward 1.0) and *every* nop run must
fail (reward 0.0), and both kinds must be present. Any missing kind, wrong
verdict, partial credit, or infra error
(``reward is None``) makes the task faulty. No DB needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.analyze.models import BaselineResult, BaselineValidation  # noqa: E402
from oddish.core.baseline_gate import (  # noqa: E402
    GATE_SKIP_MESSAGE,
    GateOutcome,
    evaluate_baseline_gate,
)


@pytest.mark.parametrize(
    "results, expected",
    [
        # Clean baselines validate the task.
        ([("oracle", 1.0), ("nop", 0.0)], GateOutcome.VALID),
        ([("oracle", 1.0), ("oracle", 1.0), ("nop", 0.0)], GateOutcome.VALID),
        # Suffixed/prefixed baseline variants still classify.
        ([("oracle-v2", 1.0), ("agent-nop", 0.0)], GateOutcome.VALID),
        # Both controls are required even when the one present is clean.
        ([("oracle", 1.0)], GateOutcome.FAULTY),
        ([("nop", 0.0)], GateOutcome.FAULTY),
        # Errors are now disqualifying: not every oracle/nop run landed cleanly.
        ([("oracle", 1.0), ("oracle", None), ("nop", 0.0)], GateOutcome.FAULTY),
        ([("oracle", 1.0), ("nop", 0.0), ("nop", None)], GateOutcome.FAULTY),
        ([("oracle", None), ("nop", 0.0)], GateOutcome.FAULTY),
        ([("oracle", 1.0), ("nop", None)], GateOutcome.FAULTY),
        # Inverted outcomes -> faulty task.
        ([("oracle", 0.0), ("nop", 0.0)], GateOutcome.FAULTY),
        ([("oracle", 1.0), ("nop", 1.0)], GateOutcome.FAULTY),
        # Oracle partial credit is not a clean pass.
        ([("oracle", 0.5), ("nop", 0.0)], GateOutcome.FAULTY),
        # One fail among passes still fails (all must pass).
        ([("oracle", 1.0), ("oracle", 0.0), ("nop", 0.0)], GateOutcome.FAULTY),
        # No baseline present at all -> inconclusive -> faulty.
        ([("oracle", None), ("nop", None)], GateOutcome.FAULTY),
        ([("claude-code", 1.0)], GateOutcome.FAULTY),
        ([], GateOutcome.FAULTY),
    ],
)
def test_evaluate_baseline_gate_outcomes(results, expected):
    outcome, reason = evaluate_baseline_gate(results)
    assert outcome is expected
    assert isinstance(reason, str) and reason


def test_faulty_reason_is_the_skip_message():
    _, reason = evaluate_baseline_gate([("oracle", 0.0), ("nop", 0.0)])
    assert reason == GATE_SKIP_MESSAGE


def test_error_in_oracle_makes_task_faulty():
    # A single oracle infra error is no longer ignored -- the task is faulty
    # even though the other oracle run passed and nop failed cleanly.
    outcome, reason = evaluate_baseline_gate(
        [("oracle", 1.0), ("oracle", None), ("nop", 0.0)]
    )
    assert outcome is GateOutcome.FAULTY
    assert reason == GATE_SKIP_MESSAGE


def test_faulty_reason_is_uniform_across_cases():
    # An inverted verdict and no-baseline-present now yield the SAME uniform
    # skip message (metrics/UI key off the SKIPPED status, not this text).
    _, inverted = evaluate_baseline_gate([("oracle", 0.0), ("nop", 0.0)])
    _, no_baselines = evaluate_baseline_gate([])
    assert inverted == GATE_SKIP_MESSAGE == no_baselines


@pytest.mark.parametrize(
    "validation, expected_issues",
    [
        (
            BaselineValidation(
                nop=BaselineResult(agent="nop", passed=False, reward=0.0),
                oracle=BaselineResult(agent="oracle", passed=True, reward=1.0),
            ),
            [],
        ),
        (
            BaselineValidation(
                oracle=BaselineResult(agent="oracle", passed=True, reward=1.0)
            ),
            ["CRITICAL: nop agent missing - task was not checked for free credit"],
        ),
        (
            BaselineValidation(
                nop=BaselineResult(agent="nop", passed=False, reward=0.0)
            ),
            [
                "CRITICAL: oracle agent missing - reference solution was not validated"
            ],
        ),
        (
            BaselineValidation(),
            [
                "CRITICAL: nop agent missing - task was not checked for free credit",
                "CRITICAL: oracle agent missing - reference solution was not validated",
            ],
        ),
        (
            BaselineValidation(
                nop=BaselineResult(agent="nop", passed=False, reward=None),
                oracle=BaselineResult(agent="oracle", passed=True, reward=1.0),
            ),
            [
                "CRITICAL: nop baseline invalid - expected a failing run with reward 0.0"
            ],
        ),
        (
            BaselineValidation(
                nop=BaselineResult(agent="nop", passed=False, reward=0.5),
                oracle=BaselineResult(agent="oracle", passed=True, reward=1.0),
            ),
            [
                "CRITICAL: nop baseline invalid - expected a failing run with reward 0.0"
            ],
        ),
        (
            BaselineValidation(
                nop=BaselineResult(agent="nop", passed=False, reward=0.0),
                oracle=BaselineResult(agent="oracle", passed=True, reward=0.5),
            ),
            [
                "CRITICAL: oracle baseline invalid - expected a passing run with reward 1.0"
            ],
        ),
        (
            BaselineValidation(
                nop=BaselineResult(agent="nop", passed=True, reward=1.0),
                oracle=BaselineResult(agent="oracle", passed=False, reward=0.0),
            ),
            [
                "CRITICAL: nop baseline invalid - expected a failing run with reward 0.0",
                "CRITICAL: oracle baseline invalid - expected a passing run with reward 1.0",
            ],
        ),
    ],
)
def test_baseline_validation_requires_both_kinds(validation, expected_issues):
    assert validation.is_valid is (not expected_issues)
    assert validation.issues == expected_issues
