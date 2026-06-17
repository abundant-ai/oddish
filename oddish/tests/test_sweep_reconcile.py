"""Unit tests for the reconcile-to-N logic in ``build_trial_specs_from_sweep``.

These are pure-function tests (no DB): they pin down the shortfall arithmetic
and the ``(agent, normalized-model)`` keying that make a manifest re-trigger
idempotent. The end-to-end / DB-level behavior (current-version scoping,
soft-delete exclusion, full wiring) is covered by ``test_double_submit_bug.py``.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.sweeps import build_trial_specs_from_sweep  # noqa: E402
from oddish.schemas import AgentModelPair, TaskSweepSubmission  # noqa: E402

AGENT = "claude-code"
MODEL = "anthropic/claude-sonnet-4-6"


def _sweep(agent: str, model: str | None, n_trials: int) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="t",
        configs=[AgentModelPair(agent=agent, model=model, n_trials=n_trials)],
    )


@pytest.mark.parametrize(
    "existing, expected",
    [
        (0, 3),  # reconcile, nothing yet           -> full N
        (1, 2),  # partial top-up (self-heal)        -> only the shortfall
        (3, 0),  # already at N (idempotent)         -> add nothing
        (5, 0),  # over N                            -> clamp at 0, never negative
    ],
)
def test_reconcile_shortfall(existing, expected):
    """n = max(0, n_trials - existing), keyed by the normalized model."""
    norm = settings.normalize_trial_model(AGENT, MODEL)
    # An absent pair must read as 0 existing (the .get default), so seed the
    # dict only when there is something to seed.
    counts = {(AGENT, norm): existing} if existing else {}
    specs = build_trial_specs_from_sweep(_sweep(AGENT, MODEL, 3), existing_counts=counts)
    assert len(specs) == expected
