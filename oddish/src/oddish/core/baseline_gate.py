"""Baseline gate decision: should a task's LLM trials run?

A task that mixes ``nop``/``oracle`` baselines with LLM agents only yields a
clean evaluation when the baselines validate the *task itself*: ``oracle``
(applies the known solution) must pass and ``nop`` (does nothing) must fail. If
they don't, the task code is faulty/flaky and the LLM trials should not run.

This module holds the pure decision: given the baselines' ``(agent, reward)``
outcomes it returns VALID or FAULTY plus a human-readable reason. The scheduling
side effects (unblock vs. cancel) live in ``oddish.queue``.

Decision rule -- every baseline run must land cleanly; errors count against it:
  - oracle must score exactly ``1.0`` on *every* run (it applies the known
    solution, so anything short of a full pass means the solution/verifier is
    broken) and nop must score exactly ``0.0`` on *every* run (it does nothing,
    so *any* nonzero reward means the task hands out credit for free -- an
    over-lenient verifier). Partial credit on either baseline is a faulty task.
  - A run that errored (``reward is None``) is NOT ignored: we can't confirm it
    landed cleanly, so any error makes the task FAULTY. (This is stricter than
    the earlier "ignore infra errors" rule -- we now require *all* oracle runs
    to pass and *all* nop runs to fail, with zero errors.)
  - The gate is VALID iff every baseline kind present passed for all its runs.
    Anything else -- a wrong verdict, partial credit, or any error -- is FAULTY
    and the LLM trials are not run. When no baseline is present at all the task
    is likewise FAULTY (nothing validated it).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from harbor.models.agent.name import AgentName

from oddish.config import nop_oracle_kind

# Prefix shared by every error_message we stamp on a gated (skipped) trial, so
# they are greppable and distinguishable from user cancellations.
GATE_SKIP_PREFIX = "Skipped by baseline gate"
# A present baseline kind that didn't land cleanly on every run -- a wrong
# verdict, partial credit, or an infra error (all disqualifying now).
GATE_FAULTY_INVERTED_MSG = f"{GATE_SKIP_PREFIX}: baseline outcome not clean"
# No nop/oracle baseline was present at all, so nothing validated the task.
GATE_FAULTY_INCONCLUSIVE_MSG = (
    f"{GATE_SKIP_PREFIX}: baselines inconclusive (no clean verdict)"
)


class GateOutcome(str, Enum):
    """Result of evaluating a task's baselines."""

    VALID = "valid"
    FAULTY = "faulty"


def evaluate_baseline_gate(
    results: Iterable[tuple[str | None, float | None]],
) -> tuple[GateOutcome, str]:
    """Decide whether a task's baselines validate it.

    ``results`` is the baselines' ``(agent, reward)`` outcomes. Returns the
    outcome and a reason string suitable for a trial ``error_message``.
    """
    rewards_by_kind: dict[str, list[float | None]] = {
        AgentName.ORACLE.value: [],
        AgentName.NOP.value: [],
    }
    for agent, reward in results:
        kind = nop_oracle_kind(agent)
        if kind is not None:
            rewards_by_kind[kind].append(reward)

    present = {kind: rs for kind, rs in rewards_by_kind.items() if rs}
    if not present:
        return GateOutcome.FAULTY, GATE_FAULTY_INCONCLUSIVE_MSG

    faults: list[str] = []
    for kind, rewards in present.items():
        # Every run of a present baseline kind must land cleanly at its extreme.
        # An infra error (reward is None) is NOT ignored -- ``None == 1`` /
        # ``None == 0`` are both False, so any error fails the check just like a
        # wrong verdict does.
        if kind == AgentName.ORACLE.value and not all(r == 1 for r in rewards):
            faults.append(f"oracle did not all pass (rewards={rewards})")
        elif kind == AgentName.NOP.value and not all(r == 0 for r in rewards):
            faults.append(f"nop did not all fail (rewards={rewards})")

    if faults:
        detail = "; ".join(faults)
        return GateOutcome.FAULTY, f"{GATE_FAULTY_INVERTED_MSG} — {detail}"
    return GateOutcome.VALID, "baselines validated (all oracle passed, all nop failed)"


__all__ = [
    "GATE_FAULTY_INCONCLUSIVE_MSG",
    "GATE_FAULTY_INVERTED_MSG",
    "GATE_SKIP_PREFIX",
    "GateOutcome",
    "evaluate_baseline_gate",
]
