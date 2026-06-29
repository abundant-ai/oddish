"""Baseline gate decision: should a task's LLM trials run?

A task that mixes ``nop``/``oracle`` baselines with LLM agents only yields a
clean evaluation when the baselines validate the *task itself*: ``oracle``
(applies the known solution) must pass and ``nop`` (does nothing) must fail. If
they don't, the task code is faulty/flaky and the LLM trials should not run.

This module holds the pure decision: given the baselines' ``(agent, reward)``
outcomes it returns VALID or FAULTY plus a human-readable reason. The scheduling
side effects (unblock vs. cancel) live in ``oddish.queue``.

Decision rule -- unanimity over *real verdicts*, infra errors ignored:
  - A trial is a real verdict iff ``reward is not None``. ``reward is None`` is
    a terminal infra error and carries no signal.
  - A baseline is "clean" only at the extremes: oracle must score exactly
    ``1.0`` (it applies the known solution, so anything short of a full pass
    means the solution/verifier is broken) and nop must score exactly ``0.0``
    (it does nothing, so *any* nonzero reward means the task hands out credit
    for free -- an over-lenient verifier). Partial credit on either baseline is
    therefore treated as a faulty task, not a clean pass/fail.
  - oracle is VALID iff it has >=1 real verdict and every real verdict == 1.0.
  - nop is VALID iff it has >=1 real verdict and every real verdict == 0.0.
  - The gate is VALID iff every baseline kind present is VALID. Anything else
    (inverted outcome, flaky, or all-errored) is FAULTY. The "all errored /
    inconclusive" case folds into FAULTY for now but is reported distinctly so a
    future hold-and-retry policy can split it out.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from harbor.models.agent.name import AgentName

# Prefix shared by every error_message we stamp on a gated (skipped) trial, so
# they are greppable and distinguishable from user cancellations.
GATE_SKIP_PREFIX = "Skipped by baseline gate"
GATE_FAULTY_INVERTED_MSG = f"{GATE_SKIP_PREFIX}: baseline outcome inverted"
GATE_FAULTY_INCONCLUSIVE_MSG = (
    f"{GATE_SKIP_PREFIX}: baselines inconclusive (no clean verdict)"
)


class GateOutcome(str, Enum):
    """Result of evaluating a task's baselines."""

    VALID = "valid"
    FAULTY = "faulty"


def _baseline_kind(agent: str | None) -> str | None:
    """Classify a baseline agent as ``'oracle'`` or ``'nop'`` (else ``None``).

    Mirrors ``is_nop_oracle_agent``: matches the canonical names plus the common
    suffixed/prefixed variants (``oracle-v2``, ``agent-nop``).
    """
    normalized = (agent or "").strip().lower()
    if not normalized:
        return None
    if AgentName.ORACLE.value in normalized:
        return AgentName.ORACLE.value
    if AgentName.NOP.value in normalized:
        return AgentName.NOP.value
    return None


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
        kind = _baseline_kind(agent)
        if kind is not None:
            rewards_by_kind[kind].append(reward)

    present = {kind: rs for kind, rs in rewards_by_kind.items() if rs}
    if not present:
        return GateOutcome.FAULTY, GATE_FAULTY_INCONCLUSIVE_MSG

    inverted: list[str] = []
    inconclusive: list[str] = []
    for kind, rewards in present.items():
        real = [r for r in rewards if r is not None]
        if not real:
            inconclusive.append(f"all {kind} trials errored")
            continue
        if kind == AgentName.ORACLE.value and not all(r == 1 for r in real):
            inverted.append(f"oracle did not pass cleanly (rewards={real})")
        elif kind == AgentName.NOP.value and not all(r == 0 for r in real):
            inverted.append(f"nop did not fail cleanly (rewards={real})")

    if inverted:
        detail = "; ".join(inverted + inconclusive)
        return GateOutcome.FAULTY, f"{GATE_FAULTY_INVERTED_MSG} — {detail}"
    if inconclusive:
        detail = "; ".join(inconclusive)
        return GateOutcome.FAULTY, f"{GATE_FAULTY_INCONCLUSIVE_MSG} — {detail}"
    return GateOutcome.VALID, "baselines validated (oracle passed, nop failed)"


__all__ = [
    "GATE_FAULTY_INCONCLUSIVE_MSG",
    "GATE_FAULTY_INVERTED_MSG",
    "GATE_SKIP_PREFIX",
    "GateOutcome",
    "evaluate_baseline_gate",
]
