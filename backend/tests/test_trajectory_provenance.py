"""Provenance is counted, and reports unknown rather than guessing."""
from __future__ import annotations

from api.services.blocks.analyzer.trajectory.provenance import (
    authored_paths_by_step,
    component_provenance,
    provenance_capable,
)


def _traj(agent, steps):
    return {"agent": agent, "steps": steps}


def _step(step_id, calls):
    return {"step_id": step_id, "tool_calls": calls}


def _call(name, **args):
    return {"function_name": name, "arguments": args}


def test_shell_only_agent_reports_unknown_not_false():
    """codex/mini-swe-agent/terminus-2 write through shell strings.

    False would read as "did not revisit its own work"; the truth is that we
    cannot see. Same rule delegation_facts uses for non-delegating agents.
    """
    traj = _traj("codex", [_step(1, [_call("shell", command="python solve.py")])])
    assert provenance_capable(traj) is False
    facts = component_provenance(traj, [(0, traj["steps"][0])])
    assert facts["provenance_capable"] is False
    assert facts["revisits_own_edits"] is None
    assert facts["runs_own_artifacts"] is None


def test_authored_paths_are_strictly_prior():
    """The step that creates a file is authoring it, not revisiting it."""
    traj = _traj(
        "claude-code",
        [
            _step(1, [_call("Write", file_path="/a/x.py")]),
            _step(2, [_call("Edit", file_path="/a/x.py")]),
        ],
    )
    prior = authored_paths_by_step(traj)
    assert prior[1] == set()
    assert prior[2] == {"/a/x.py"}


def test_revisiting_own_edit_is_detected():
    traj = _traj(
        "claude-code",
        [
            _step(1, [_call("Write", file_path="/a/x.py")]),
            _step(2, [_call("Edit", file_path="/a/x.py")]),
        ],
    )
    facts = component_provenance(traj, [(1, traj["steps"][1])])
    assert facts["revisits_own_edits"] is True


def test_first_write_alone_is_not_a_revisit():
    traj = _traj("claude-code", [_step(1, [_call("Write", file_path="/a/x.py")])])
    facts = component_provenance(traj, [(0, traj["steps"][0])])
    assert facts["revisits_own_edits"] is None


def test_running_an_agent_authored_script_is_detected():
    traj = _traj(
        "claude-code",
        [
            _step(1, [_call("Write", file_path="/workdir/probe.py")]),
            _step(2, [_call("Bash", command="python /workdir/probe.py --v")]),
        ],
    )
    facts = component_provenance(traj, [(1, traj["steps"][1])])
    assert facts["runs_own_artifacts"] is True


def test_running_a_provided_checker_is_not_agent_authored():
    traj = _traj(
        "claude-code",
        [
            _step(1, [_call("Write", file_path="/workdir/probe.py")]),
            _step(2, [_call("Bash", command="pytest tests/test_public.py")]),
        ],
    )
    facts = component_provenance(traj, [(1, traj["steps"][1])])
    assert facts["runs_own_artifacts"] is None


def test_short_paths_do_not_substring_match():
    """`a.py` inside `data.py` would be a false positive."""
    traj = _traj(
        "claude-code",
        [
            _step(1, [_call("Write", file_path="a.py")]),
            _step(2, [_call("Bash", command="python data.py")]),
        ],
    )
    facts = component_provenance(traj, [(1, traj["steps"][1])])
    assert facts["runs_own_artifacts"] is None


def test_component_steps_carry_positions_not_step_ids():
    """to_summary passes (enumerate_index, step); the id comes off the dict.

    Keying on the tuple's first element silently reads position 1 as step_id 1.
    """
    traj = _traj(
        "claude-code",
        [
            _step(101, [_call("Write", file_path="/a/x.py")]),
            _step(102, [_call("Edit", file_path="/a/x.py")]),
        ],
    )
    facts = component_provenance(traj, [(1, traj["steps"][1])])
    assert facts["revisits_own_edits"] is True


def test_per_agent_tool_names():
    """Each capable agent's own write vocabulary is recognised."""
    for agent, tool, key in (
        ("gemini-cli", "write_file", "file_path"),
        ("grok-build", "write", "file_path"),
        ("opencode", "write", "filePath"),
    ):
        traj = _traj(
            agent,
            [
                _step(1, [{"function_name": tool, "arguments": {key: "/a/long_name.py"}}]),
                _step(2, [{"function_name": tool, "arguments": {key: "/a/long_name.py"}}]),
            ],
        )
        assert provenance_capable(traj) is True, agent
        facts = component_provenance(traj, [(1, traj["steps"][1])])
        assert facts["revisits_own_edits"] is True, agent


def test_name_key_falls_back_like_delegation():
    traj = _traj(
        "claude-code",
        [
            _step(1, [{"name": "Write", "arguments": {"file_path": "/a/x.py"}}]),
            _step(2, [{"name": "Edit", "arguments": {"file_path": "/a/x.py"}}]),
        ],
    )
    assert component_provenance(traj, [(1, traj["steps"][1])])["revisits_own_edits"]


def test_malformed_input_is_survivable():
    assert provenance_capable({}) is False
    assert authored_paths_by_step(None) == {}
    facts = component_provenance(
        _traj("claude-code", [None, _step(1, ["not-a-dict"])]), [(0, None)]
    )
    assert facts["revisits_own_edits"] is None
