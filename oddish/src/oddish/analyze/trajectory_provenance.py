"""File provenance counted from a trajectory, never inferred by a model.

Sibling of :mod:`trajectory_delegation`, for the same reason and by the same rule: a fact
that is arithmetic over tool calls should not be a judgement call.

The labels that ask for provenance are the least reproducible in the taxonomy.
Summarizing 100 trials twice under identical input and comparing per step:

    plan_correction          36.4% reproducible   ("needs an earlier plan")
    implementing_correction  59.6%                ("the agent's OWN earlier edit")
    testing_custom           66.3%                ("scripts the agent wrote itself")

against `reading_files` at 78.1% and `writing_report` at 87.3%. The pattern is
not difficulty, it is bookkeeping: each of those definitions asks the model to
remember, across a 300-step trajectory in a single pass, who authored a thing.
That is a scan, and a scan is what code is for.

Only agents with structured file tools can be measured this way. `codex`
(`shell`), `mini-swe-agent` (`bash`) and `terminus-2` (`bash_command`) route
every write through a shell string, so their provenance is genuinely unknown
and is reported as None -- never as False, which would read as "the agent did
not revisit its own work" when the truth is that we cannot see.

The same caution applies WITHIN a capable agent: claude-code issues far more
Bash calls than Edit/Write calls, so a file created by a heredoc is invisible
here. Every field is therefore True-or-None. A positive is evidence; the
absence of one is not evidence of absence.
"""

from __future__ import annotations

from typing import Any

from oddish.analyze.trajectory_tool_calls import (
    COMMAND_ARGUMENT_KEYS,
    PATH_ARGUMENT_KEYS,
    string_argument,
    tool_name,
)

# Structured file tools per agent, by the names real trajectories record.
# An agent absent from this map has no structured file tooling and cannot be
# measured -- see the module docstring.
_WRITE_TOOLS: dict[str, frozenset[str]] = {
    "claude-code": frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"}),
    "gemini-cli": frozenset({"write_file", "replace"}),
    "antigravity-cli": frozenset(
        {
            "write_to_file",
            "edit_file",
            "replace_file_content",
            "multi_replace_file_content",
        }
    ),
    "grok-build": frozenset({"write", "search_replace"}),
    "opencode": frozenset({"write", "edit"}),
}

# Tools whose arguments carry a command string rather than a path.
_RUN_TOOLS = frozenset(
    {
        "Bash",
        "bash",
        "shell",
        "bash_command",
        "run_shell_command",
        "run_terminal_command",
    }
)

# A path shorter than this matches too much inside a command string ("a.py"
# would hit "data.py"). Short paths are skipped rather than guessed at.
_MIN_PATH_MATCH = 6


def _agent_name(trajectory: dict) -> str | None:
    agent = (trajectory or {}).get("agent")
    if isinstance(agent, dict):
        name = agent.get("name")
        return name if isinstance(name, str) else None
    return agent if isinstance(agent, str) else None


def provenance_capable(trajectory: dict) -> bool:
    """Whether this run's agent exposes structured file tools at all."""
    return (
        _agent_name(trajectory if isinstance(trajectory, dict) else {}) in _WRITE_TOOLS
    )


def authored_paths_by_step(trajectory: dict) -> dict[int, set[str]]:
    """``step_id -> paths the agent had already written BEFORE that step``.

    Strictly "before": a step that creates a file is authoring it, not
    revisiting it, so the set handed to a step never includes that step's own
    writes. Steps are walked in recorded order, which is the order the ATIF
    writer emitted them.
    """
    if not isinstance(trajectory, dict):
        return {}
    agent = _agent_name(trajectory)
    write_tools = _WRITE_TOOLS.get(agent or "")
    if not write_tools:
        return {}

    seen: set[str] = set()
    out: dict[int, set[str]] = {}
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_id = step.get("step_id")
        if isinstance(step_id, int):
            out[step_id] = set(seen)
        for call in step.get("tool_calls") or []:
            if tool_name(call) in write_tools:
                path = string_argument(call, PATH_ARGUMENT_KEYS)
                if path:
                    seen.add(path)
    return out


def component_provenance(
    trajectory: dict, component_steps: list[tuple[Any, dict]]
) -> dict[str, Any]:
    """Provenance facts for one component's steps.

    ``component_steps`` carries ``(enumerate_index, step)`` pairs, matching
    what ``to_summary`` already builds -- the first element is a POSITION, not
    a ``step_id``. The step id is read off the step dict so this cannot go
    wrong if the caller's tuple shape ever changes.

    ``revisits_own_edits``  -- this component edits a path an EARLIER step of
                               the same run already wrote. The evidence
                               `implementing_correction` is guessing at.
    ``runs_own_artifacts``  -- this component runs a command naming a path the
                               agent wrote earlier. The evidence separating
                               `testing_custom` from `testing_public`.

    Both are True or None, never False -- see the module docstring.
    """
    if not provenance_capable(trajectory):
        return {
            "provenance_capable": False,
            "revisits_own_edits": None,
            "runs_own_artifacts": None,
        }

    agent = _agent_name(trajectory)
    write_tools = _WRITE_TOOLS.get(agent or "", frozenset())
    prior = authored_paths_by_step(trajectory)

    revisits = False
    runs_own = False
    for _position, step in component_steps:
        if not isinstance(step, dict):
            continue
        already = prior.get(step.get("step_id"), set())
        for call in step.get("tool_calls") or []:
            name = tool_name(call)
            if name in write_tools:
                path = string_argument(call, PATH_ARGUMENT_KEYS)
                if path and path in already:
                    revisits = True
            elif name in _RUN_TOOLS:
                command = string_argument(call, COMMAND_ARGUMENT_KEYS)
                if command and any(
                    len(p) >= _MIN_PATH_MATCH and p in command for p in already
                ):
                    runs_own = True

    return {
        "provenance_capable": True,
        "revisits_own_edits": True if revisits else None,
        "runs_own_artifacts": True if runs_own else None,
    }
