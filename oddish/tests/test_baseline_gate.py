"""Unit tests for the pure baseline-gate decision.

``evaluate_baseline_gate`` decides whether a task's nop/oracle baselines
validate it: oracle must pass, nop must fail, judged by unanimity over real
verdicts (``reward is not None``) with infra errors ignored. No DB needed.
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
        # Errors carry no signal; a clean real verdict still validates.
        ([("oracle", 1.0), ("oracle", None), ("nop", 0.0)], GateOutcome.VALID),
        # Inverted outcomes -> faulty task.
        ([("oracle", 0.0), ("nop", 0.0)], GateOutcome.FAULTY),
        ([("oracle", 1.0), ("nop", 1.0)], GateOutcome.FAULTY),
        # Oracle partial credit is not a clean pass.
        ([("oracle", 0.5), ("nop", 0.0)], GateOutcome.FAULTY),
        # One real fail among passes still fails (unanimity).
        ([("oracle", 1.0), ("oracle", 0.0), ("nop", 0.0)], GateOutcome.FAULTY),
        # No real verdict at all -> inconclusive -> faulty (for now).
        ([("oracle", None), ("nop", 0.0)], GateOutcome.FAULTY),
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


def test_inverted_vs_inconclusive_reasons_differ():
    _, inverted = evaluate_baseline_gate([("oracle", 0.0), ("nop", 0.0)])
    _, inconclusive = evaluate_baseline_gate([("oracle", None), ("nop", None)])
    assert "inverted" in inverted
    assert "inconclusive" in inconclusive
