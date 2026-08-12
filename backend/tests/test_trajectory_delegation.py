from api.services.blocks.analyzer.trajectory.delegation import (
    delegation_facts,
    subagent_dispatches_in,
)


def _traj(agent: str, steps: list[dict]) -> dict:
    return {"agent": {"name": agent}, "steps": steps}


def _call(name: str) -> dict:
    return {"tool_call_id": "x", "function_name": name, "arguments": "{}"}


def test_counts_the_agent_tool():
    traj = _traj(
        "claude-code",
        [
            {"step_id": 1, "tool_calls": [_call("Read")]},
            {"step_id": 2, "tool_calls": [_call("Agent"), _call("Agent")]},
        ],
    )
    facts = delegation_facts(traj)
    assert facts["capable"] is True
    assert facts["dispatches"] == 2


def test_counts_the_legacy_task_spelling():
    # The probe overlay still calls it the Task tool, and older claude-code
    # trajectories record that spelling.
    traj = _traj("claude-code", [{"step_id": 1, "tool_calls": [_call("Task")]}])
    assert delegation_facts(traj)["dispatches"] == 1


def test_a_claude_code_run_that_never_delegated_is_a_real_zero():
    traj = _traj("claude-code", [{"step_id": 1, "tool_calls": [_call("Bash")]}])
    facts = delegation_facts(traj)
    assert facts["capable"] is True
    assert facts["dispatches"] == 0


def test_an_agent_without_a_subagent_tool_is_not_a_zero():
    # This is the whole point of `capable`. codex and gemini-cli have no
    # subagent tool, so reporting 0 would invite the comparison to "discover"
    # that failing runs never delegate, when they simply could not.
    for agent in ("codex", "gemini-cli"):
        facts = delegation_facts(
            _traj(agent, [{"step_id": 1, "tool_calls": [_call("shell")]}])
        )
        assert facts["capable"] is False, agent
        assert facts["dispatches"] is None, agent


def test_unknown_agent_is_treated_as_incapable_rather_than_zero():
    facts = delegation_facts(_traj("some-new-agent", [{"step_id": 1}]))
    assert facts["capable"] is False
    assert facts["dispatches"] is None


def test_counts_captured_sidechain_steps():
    traj = _traj(
        "claude-code",
        [
            {"step_id": 1, "extra": {"is_sidechain": False}},
            {"step_id": 2, "extra": {"is_sidechain": True}},
            {"step_id": 3, "extra": {"is_sidechain": True}},
        ],
    )
    assert delegation_facts(traj)["sidechain_steps"] == 2


def test_sidechain_steps_can_be_zero_while_dispatches_are_not():
    # Observed in real data: 6 Agent calls across trials whose trajectories
    # record no sidechain steps at all. The subagent's inner work was never
    # captured, so the two numbers must be reported separately.
    traj = _traj(
        "claude-code",
        [{"step_id": 1, "tool_calls": [_call("Agent")], "extra": {"is_sidechain": False}}],
    )
    facts = delegation_facts(traj)
    assert facts["dispatches"] == 1
    assert facts["sidechain_steps"] == 0


def test_falls_back_to_the_name_key():
    # harbor_artifacts.py:301 reads `function_name or name`; match it, because
    # a silently-missed key is the defect this whole feature exists to close.
    traj = _traj("claude-code", [{"step_id": 1, "tool_calls": [{"name": "Agent"}]}])
    assert delegation_facts(traj)["dispatches"] == 1


def test_tolerates_a_malformed_trajectory():
    assert delegation_facts({})["capable"] is False
    assert delegation_facts({"agent": None, "steps": None})["dispatches"] is None
    assert (
        delegation_facts(
            {"agent": {"name": "claude-code"}, "steps": [None, {"tool_calls": None}]}
        )["dispatches"]
        == 0
    )


def test_subagent_dispatches_in_counts_a_step_subset():
    steps = [
        {"step_id": 1, "tool_calls": [_call("Agent")]},
        {"step_id": 2, "tool_calls": [_call("Bash")]},
    ]
    assert subagent_dispatches_in(steps) == 1
    assert subagent_dispatches_in(steps[1:]) == 0
