from __future__ import annotations

import asyncio
from types import SimpleNamespace

from oddish.analyze import trajectory_graph as tg
from oddish.core import trial_io
from oddish.db.models import TrialModel


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


# --- persistence layer ---


def test_trajectory_graph_column_is_mapped():
    trial = TrialModel()
    assert "trajectory_graph" in TrialModel.__table__.columns
    assert trial.trajectory_graph is None
    payload = {"schema_version": tg.GRAPH_SCHEMA_VERSION, "steps": [], "terminal": {}}
    trial.trajectory_graph = payload
    assert trial.trajectory_graph == payload


def test_read_persisted_only_returns_fresh():
    trial = SimpleNamespace(trajectory_graph=None)
    assert trial_io.read_persisted_trajectory_graph(trial) is None

    trial.trajectory_graph = {"schema_version": "0", "steps": []}  # stale
    assert trial_io.read_persisted_trajectory_graph(trial) is None

    fresh = {"schema_version": tg.GRAPH_SCHEMA_VERSION, "steps": [{"id": "s0"}]}
    trial.trajectory_graph = fresh
    assert trial_io.read_persisted_trajectory_graph(trial) == fresh


class _FakeSession:
    def __init__(self):
        self.committed = False
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.committed = True


def test_generate_and_store_persists_and_stamps(monkeypatch):
    async def _fake_read_trajectory(trial):
        return {
            "steps": [
                {"step_id": "1", "source": "agent", "tool_calls": [{"function_name": "bash"}]},
            ]
        }

    async def _fake_instruction(trial):
        return "Fix the handler."

    async def _fake_verifier(trial):
        return "FAIL: expected X, got Y"

    monkeypatch.setattr(trial_io, "read_trial_trajectory", _fake_read_trajectory)
    monkeypatch.setattr(trial_io, "read_trial_instruction", _fake_instruction)
    monkeypatch.setattr(trial_io, "read_trial_verifier_output", _fake_verifier)

    trial = SimpleNamespace(
        id="trial-1",
        status="success",
        reward=0.0,
        error_message=None,
        name="demo/task",
        agent="codex",
        trajectory_graph=None,
    )
    session = _FakeSession()
    graph = asyncio.run(
        trial_io.generate_and_store_trajectory_graph(session, trial)
    )
    assert graph["schema_version"] == tg.GRAPH_SCHEMA_VERSION
    assert graph["terminal"]["outcome"] == tg.OUTCOME_FAILURE
    assert "generated_at" in graph
    assert session.committed is True
    assert len(session.executed) == 1


def test_generate_returns_cached_when_fresh_and_not_refresh(monkeypatch):
    calls = {"n": 0}

    async def _fake_read_trajectory(trial):
        calls["n"] += 1
        return {"steps": []}

    monkeypatch.setattr(trial_io, "read_trial_trajectory", _fake_read_trajectory)

    fresh = {"schema_version": tg.GRAPH_SCHEMA_VERSION, "steps": [], "terminal": {}}
    trial = SimpleNamespace(
        id="trial-2", status="success", reward=1.0, error_message=None,
        name="t", agent="a", trajectory_graph=fresh,
    )
    session = _FakeSession()
    graph = asyncio.run(
        trial_io.generate_and_store_trajectory_graph(session, trial)
    )
    assert graph is fresh
    assert calls["n"] == 0  # short-circuited, no trajectory read
    assert session.committed is False


# --- goal + grader grounding ---


def test_grounded_reason_names_failing_grader_line():
    ctx = {
        "verifier_output": "running checks\ngate1 ok\nAssertionError: swap_goodput 0.4 < 0.9\nDONE",
    }
    reason = tg._grounded_reason(ctx, tg.OUTCOME_FAILURE)
    assert "swap_goodput 0.4 < 0.9" in reason
    # success never appends a failing line
    assert "swap_goodput" not in tg._grounded_reason(ctx, tg.OUTCOME_SUCCESS)


def test_prompt_includes_goal_and_grader_context():
    ctx = {
        "task_name": "chain-spine/01",
        "agent_name": "codex",
        "status": "success",
        "reward": 0.0,
        "num_steps": 4,
        "task_instruction": "Fix the indexer so swap goodput recovers.",
        "verifier_output": "AssertionError: swap_goodput 0.4 < 0.9",
    }
    prompt = tg._build_prompt(ctx, tg.OUTCOME_FAILURE, "#0 tools=bash")
    assert "Fix the indexer so swap goodput recovers." in prompt
    assert "swap_goodput 0.4 < 0.9" in prompt
    assert "Task goal" in prompt and "Grader output" in prompt


def test_summary_terminal_reason_is_grounded():
    summary = {
        "phases": [{"label": "Patch", "step_ids": [3], "gist": "edited"}],
        "highlights": [],
    }
    ctx = {
        "status": "success", "reward": 0.0, "task_name": "t", "agent_name": "a",
        "verifier_output": "FAIL: expected reserve1 fresh, got stale",
    }
    g = asyncio.run(
        tg.build_trajectory_graph(_traj(), ctx, model=None, summary=summary)
    )
    assert "expected reserve1 fresh, got stale" in g["terminal"]["reason"]
