"""Unit tests for the pure baseline-gate decision.

``evaluate_baseline_gate`` decides whether a task's nop/oracle baselines
validate it: *every* oracle run must pass (reward 1.0) and *every* nop run must
fail (reward 0.0). Any wrong verdict, partial credit, or infra error
(``reward is None``) makes the task faulty. No DB needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.baseline_gate import (  # noqa: E402
    GATE_SKIP_PREFIX,
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
        # Only one baseline kind present -> gate on what we have.
        ([("oracle", 1.0)], GateOutcome.VALID),
        ([("nop", 0.0)], GateOutcome.VALID),
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


def test_faulty_reason_is_greppable():
    _, reason = evaluate_baseline_gate([("oracle", 0.0), ("nop", 0.0)])
    assert GATE_SKIP_PREFIX in reason


def test_error_in_oracle_makes_task_faulty():
    # A single oracle infra error is no longer ignored -- the task is faulty
    # even though the other oracle run passed and nop failed cleanly.
    outcome, reason = evaluate_baseline_gate(
        [("oracle", 1.0), ("oracle", None), ("nop", 0.0)]
    )
    assert outcome is GateOutcome.FAULTY
    assert GATE_SKIP_PREFIX in reason


def test_not_clean_vs_no_baselines_reasons_differ():
    _, not_clean = evaluate_baseline_gate([("oracle", 0.0), ("nop", 0.0)])
    _, no_baselines = evaluate_baseline_gate([])
    assert "not clean" in not_clean
    assert "inconclusive" in no_baselines
