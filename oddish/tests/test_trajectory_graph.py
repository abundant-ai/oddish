from __future__ import annotations

import asyncio

from oddish.analyze import trajectory_graph as tg


def _traj():
    return {
        "schema_version": "ATIF-v1.5",
        "steps": [
            {
                "step_id": "1",
                "source": "agent",
                "reasoning_content": "look at the repo",
                "tool_calls": [{"function_name": "bash"}],
                "observation": {"results": [{"content": "file list"}]},
            },
            {"step_id": "2", "source": "agent", "tool_calls": [{"function_name": "read_file"}]},
            {
                "step_id": "3",
                "source": "agent",
                "message": "patching the handler",
                "tool_calls": [{"function_name": "edit_file"}],
            },
            {
                "step_id": "4",
                "source": "agent",
                "tool_calls": [{"function_name": "bash"}],
                "observation": {"results": [{"content": "tests failed"}]},
            },
        ],
    }


def _build(ctx):
    # model=None forces the deterministic heuristic path (no network / no key).
    return asyncio.run(tg.build_trajectory_graph(_traj(), ctx, model=None))


def test_outcome_success_from_reward():
    g = _build({"status": "success", "reward": 1.0, "error_message": None, "task_name": "t", "agent_name": "a"})
    assert g["terminal"]["outcome"] == tg.OUTCOME_SUCCESS
    # a passing run should not paint the last phase red
    assert g["steps"][-1]["status"] == "ok"
    assert g["source"] == "heuristic"


def test_outcome_failure_when_completed_reward_zero():
    g = _build({"status": "success", "reward": 0.0, "error_message": None, "task_name": "t", "agent_name": "a"})
    assert g["terminal"]["outcome"] == tg.OUTCOME_FAILURE
    assert g["steps"][-1]["status"] == "error"


def test_outcome_timeout_from_error_text():
    g = _build(
        {
            "status": "failed",
            "reward": None,
            "error_message": "Agent exceeded the maximum wall clock time limit",
            "task_name": "t",
            "agent_name": "a",
        }
    )
    assert g["terminal"]["outcome"] == tg.OUTCOME_TIMEOUT


def test_outcome_error_for_harness_failure():
    g = _build({"status": "failed", "reward": None, "error_message": "sandbox crashed", "task_name": "t", "agent_name": "a"})
    assert g["terminal"]["outcome"] == tg.OUTCOME_ERROR


def test_no_trajectory_still_returns_graph():
    g = asyncio.run(
        tg.build_trajectory_graph(
            None,
            {"status": "failed", "reward": None, "error_message": "boom", "task_name": "t", "agent_name": "a"},
            model=None,
        )
    )
    assert g["steps"], "should always yield at least one node"
    assert g["terminal"]["outcome"] == tg.OUTCOME_ERROR


def test_graph_from_summary_reuses_phases_and_flags_trouble():
    summary = {
        "schema_version": "3",
        "summary": "Agent explored, patched, and the tests failed.",
        "phases": [
            {"label": "Explore", "step_ids": [1, 2], "gist": "read the repo"},
            {"label": "Patch", "step_ids": [3], "gist": "edited handler"},
            {"label": "Verify", "step_ids": [4], "gist": "ran tests"},
        ],
        "highlights": [
            {"step_id": 3, "title": "Committed a fix", "why": "edited the handler"},
            {"step_id": 4, "title": "Tests failed", "why": "assertion error surfaced"},
        ],
    }
    g = asyncio.run(
        tg.build_trajectory_graph(
            _traj(),
            {"status": "success", "reward": 0.0, "error_message": None, "task_name": "t", "agent_name": "a"},
            model=None,
            summary=summary,
        )
    )
    assert g["source"] == "summary"
    assert [s["title"] for s in g["steps"]] == ["Explore", "Patch", "Verify"]
    # the Verify phase holds step 4 (a 'Tests failed' highlight) and is the last
    # phase of a failed run -> error; earlier phases stay ok.
    assert g["steps"][0]["status"] == "ok"
    assert g["steps"][-1]["status"] == "error"
    assert g["terminal"]["outcome"] == tg.OUTCOME_FAILURE
    assert g["terminal"]["last_action"] == "Tests failed"


def test_summary_without_phases_falls_back():
    g = asyncio.run(
        tg.build_trajectory_graph(
            _traj(),
            {"status": "success", "reward": 1.0, "task_name": "t", "agent_name": "a"},
            model=None,
            summary={"schema_version": "3", "summary": "x", "phases": []},
        )
    )
    # empty phases -> not usable -> heuristic path
    assert g["source"] == "heuristic"


def test_normalize_coerces_bad_llm_output():
    ctx = {"status": "success", "reward": 0.0, "task_name": "t", "agent_name": "a", "model": "m"}
    raw = {
        "headline": "did stuff",
        "steps": [
            {"title": "explore", "detail": "looked around", "status": "ok"},
            {"title": "patch", "detail": "edited", "status": "bogus"},  # invalid status -> coerced ok
        ],
        "terminal": {"last_action": "ran tests", "reason": "grader failed"},
    }
    g = tg._normalize_graph(raw, ctx, tg.OUTCOME_FAILURE, tg._digest_steps(_traj()))
    assert [s["status"] for s in g["steps"]] == ["ok", "ok"]
    assert g["steps"][0]["id"] == "s0"
    # authoritative outcome is stamped regardless of what the LLM said
    assert g["terminal"]["outcome"] == tg.OUTCOME_FAILURE
