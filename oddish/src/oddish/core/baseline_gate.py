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
from sqlalchemy import func, or_

from oddish.config import nop_oracle_kind

# Legacy sentinel: the prefix stamped on gate-cancelled trials BEFORE they
# became a first-class SKIPPED status. Retained only so those pre-existing
# (FAILED) rows are still excluded from QA classification -- new skipped trials
# are matched by ``status == SKIPPED`` instead. Not used in new messages.
GATE_SKIP_PREFIX = "Skipped by baseline gate"

# Human-facing reason stamped on a skipped trial's ``error_message``. Uniform
# across fault kinds (a wrong verdict, partial credit, an infra error, or no
# baseline at all); QA and metrics key off the SKIPPED status rather than this
# text, so it is free to read as a plain reason.
GATE_SKIP_MESSAGE = "Trial skipped: nop/oracle validation failed"


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
        # No nop/oracle baseline present at all -> nothing validated the task.
        return GateOutcome.FAULTY, GATE_SKIP_MESSAGE

    for kind, rewards in present.items():
        # Every run of a present baseline kind must land cleanly at its extreme.
        # An infra error (reward is None) is NOT ignored -- ``None == 1`` /
        # ``None == 0`` are both False, so any error fails the check just like a
        # wrong verdict does.
        if kind == AgentName.ORACLE.value and not all(r == 1 for r in rewards):
            return GateOutcome.FAULTY, GATE_SKIP_MESSAGE
        if kind == AgentName.NOP.value and not all(r == 0 for r in rewards):
            return GateOutcome.FAULTY, GATE_SKIP_MESSAGE

    return GateOutcome.VALID, "baselines validated (all oracle passed, all nop failed)"


def baseline_agent_clause(agent_column):
    """SQL counterpart to :func:`oddish.config.is_nop_oracle_agent`.

    Callers that must filter baselines in the database share this one clause so
    the SQL and the Python predicate can't drift -- the config module's prefix
    lists, the frontend's ``isBaselineAgentName``, and this all have to agree on
    what counts as a baseline.
    """
    agent_lower = func.lower(func.coalesce(agent_column, ""))
    return or_(
        agent_lower == AgentName.NOP.value,
        agent_lower == AgentName.ORACLE.value,
        agent_lower.like("nop-%"),
        agent_lower.like("oracle-%"),
        agent_lower.like("agent-nop%"),
        agent_lower.like("agent-oracle%"),
    )


__all__ = [
    "GATE_SKIP_MESSAGE",
    "GATE_SKIP_PREFIX",
    "GateOutcome",
    "baseline_agent_clause",
    "evaluate_baseline_gate",
]
