"""Delegation facts counted from a trajectory, never inferred by a model.

Whether a run spawned subagents is arithmetic over tool calls we already hold.
Routing it through the summariser made it a lottery: the component taxonomy has
no delegation label, so a dispatch lands in ``implementing`` and the fact
survives only if the LLM happened to mention it in prose. The cohort comparison
reads component summaries and nothing else, so once the prose omitted it the
finding was unrecoverable.
"""
from __future__ import annotations

# claude-code's subagent tool. `Agent` is what current trajectories record;
# `Task` is the older spelling, and the one the probe overlay still writes
# (worker/probe_overlay.py:126). Both mean one subagent dispatched.
SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task"})

# Agents that can delegate at all. codex (`shell`) and gemini-cli
# (`run_shell_command`) have no subagent tool, so their runs must report
# `None` rather than 0 -- see `capable` below.
DELEGATING_AGENTS = frozenset({"claude-code"})


def _tool_name(call: dict) -> str | None:
    """``function_name``, falling back to ``name``.

    Mirrors core/harbor_artifacts.py:301. The fallback is not cosmetic: reading
    only `name` is exactly how per-step subagent attribution was lost before.
    """
    if not isinstance(call, dict):
        return None
    return call.get("function_name") or call.get("name")


def subagent_dispatches_in(steps) -> int:
    """How many subagents these steps spawned."""
    total = 0
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        for call in step.get("tool_calls") or []:
            if _tool_name(call) in SUBAGENT_TOOL_NAMES:
                total += 1
    return total


def _agent_name(trajectory: dict) -> str | None:
    agent = (trajectory or {}).get("agent")
    if isinstance(agent, dict):
        name = agent.get("name")
        return name if isinstance(name, str) else None
    return agent if isinstance(agent, str) else None


def delegation_facts(trajectory: dict) -> dict:
    """``{agent, capable, dispatches, sidechain_steps}`` for one run.

    ``dispatches`` is None -- not 0 -- whenever the agent has no subagent tool,
    including an agent we do not recognise. A cohort comparison spanning models
    would otherwise read "gemini-cli: 0" as a choice not to delegate and report
    a difference that is really just a difference in tooling.

    ``sidechain_steps`` counts the subagent's own captured steps, which is a
    separate question from how many were dispatched: real trajectories carry
    ``Agent`` calls while recording no sidechain steps at all, meaning the
    inner work was never captured.
    """
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    agent = _agent_name(trajectory)
    if agent not in DELEGATING_AGENTS:
        return {
            "agent": agent,
            "capable": False,
            "dispatches": None,
            "sidechain_steps": None,
        }
    steps = trajectory.get("steps") or []
    sidechain = sum(
        1
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("extra"), dict)
        and step["extra"].get("is_sidechain") is True
    )
    return {
        "agent": agent,
        "capable": True,
        "dispatches": subagent_dispatches_in(steps),
        "sidechain_steps": sidechain,
    }
