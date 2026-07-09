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
    # Detected the same way the frontend getMatrixStatus does.
    g = _build(
        {
            "status": "failed",
            "reward": None,
            "error_message": "AgentTimeoutError: Agent execution timed out after 1800s",
            "task_name": "t",
            "agent_name": "a",
        }
    )
    assert g["terminal"]["outcome"] == tg.OUTCOME_TIMEOUT


def test_outcome_error_for_harness_failure():
    g = _build({"status": "failed", "reward": None, "error_message": "sandbox crashed", "task_name": "t", "agent_name": "a"})
    assert g["terminal"]["outcome"] == tg.OUTCOME_ERROR


def test_outcome_partial_credit_not_passed():
    g = _build({"status": "success", "reward": 0.5, "error_message": None, "task_name": "t", "agent_name": "a"})
    assert g["terminal"]["outcome"] == tg.OUTCOME_PARTIAL
    # partial credit must not paint the last phase red or read as "passed"
    assert g["steps"][-1]["status"] != "error"


def test_outcome_scoreless_when_success_without_reward():
    g = _build({"status": "success", "reward": None, "error_message": None, "task_name": "t", "agent_name": "a"})
    assert g["terminal"]["outcome"] == tg.OUTCOME_SCORELESS


def test_agent_timeout_with_reward_scores_normally():
    g = _build(
        {
            "status": "failed",
            "reward": 1.0,
            "error_message": "AgentTimeoutError: timed out but a reward was recorded",
            "task_name": "t",
            "agent_name": "a",
        }
    )
    assert g["terminal"]["outcome"] == tg.OUTCOME_SUCCESS


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
    # terminal describes the actual final ATIF step (a bash call), not the last
    # highlight (which may predate the closing steps)
    assert g["terminal"]["last_action"] == "Called bash"


def test_is_trouble_word_boundary_and_negation():
    assert tg._is_trouble("raised an exception")
    assert tg._is_trouble("the build failed")
    assert tg._is_trouble("retried three times")  # startswith 'retr...'
    assert not tg._is_trouble("no terror here")  # 'error' not at a word boundary
    # negated / benign mentions must NOT flag a snag
    assert not tg._is_trouble("completed with no errors")
    assert not tg._is_trouble("0 failures, all green")
    assert not tg._is_trouble("ran to completion")  # no trouble marker present


def test_summary_used_even_with_empty_digest():
    # has_trajectory but the ATIF didn't parse into steps here; a persisted
    # summary must still drive the graph, not "No trajectory recorded".
    summary = {
        "phases": [{"label": "Investigate", "step_ids": [1], "gist": "looked"}],
        "highlights": [{"step_id": 1, "title": "Checked metrics", "why": "ok"}],
    }
    g = asyncio.run(
        tg.build_trajectory_graph(
            {"steps": []},
            {"status": "success", "reward": 0.0, "task_name": "t", "agent_name": "a"},
            model=None,
            summary=summary,
        )
    )
    assert g["source"] == "summary"
    assert [s["title"] for s in g["steps"]] == ["Investigate"]
    # no digest -> falls back to the last highlight for the action
    assert g["terminal"]["last_action"] == "Checked metrics"


def test_empty_digest_highlight_fallback_uses_max_step_id():
    # highlights NOT in step_id order; the fallback must pick the LATEST step,
    # not the last array entry.
    summary = {
        "phases": [{"label": "Work", "step_ids": [2, 9], "gist": "did work"}],
        "highlights": [
            {"step_id": 9, "title": "Final commit"},
            {"step_id": 2, "title": "Early probe"},
        ],
    }
    g = asyncio.run(
        tg.build_trajectory_graph(
            {"steps": []},
            {"status": "success", "reward": 1.0, "task_name": "t", "agent_name": "a"},
            model=None,
            summary=summary,
        )
    )
    assert g["terminal"]["last_action"] == "Final commit"


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
    # invalid status coerced to ok; then, since a FAILURE had no error phase, the
    # last phase is forced to error (all paths guarantee this).
    assert [s["status"] for s in g["steps"]] == ["ok", "error"]
    assert g["steps"][0]["id"] == "s0"
    # authoritative outcome is stamped regardless of what the LLM said
    assert g["terminal"]["outcome"] == tg.OUTCOME_FAILURE


def test_llm_path_respects_located_error_phase():
    # the model already marked an EARLIER phase as the error -> don't override it
    ctx = {"status": "success", "reward": 0.0, "task_name": "t", "agent_name": "a", "model": "m"}
    raw = {
        "headline": "h",
        "steps": [
            {"title": "wrong turn", "detail": "chased a red herring", "status": "error"},
            {"title": "gave up", "detail": "ran out of ideas", "status": "ok"},
        ],
        "terminal": {"last_action": "stopped", "reason": "x"},
    }
    g = tg._normalize_graph(raw, ctx, tg.OUTCOME_FAILURE, tg._digest_steps(_traj()))
    assert [s["status"] for s in g["steps"]] == ["error", "ok"]  # not forced onto the last


def test_reward_out_of_band_is_partial():
    # matches getRewardMatrixStatus: only exactly 1 passes, only exactly 0 fails
    assert tg._reward_outcome(1.5) == tg.OUTCOME_PARTIAL
    assert tg._reward_outcome(-0.2) == tg.OUTCOME_PARTIAL
    assert tg._reward_outcome(1.0) == tg.OUTCOME_SUCCESS
    assert tg._reward_outcome(0.0) == tg.OUTCOME_FAILURE


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
        self.refreshed = 0

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj, attribute_names=None):
        self.refreshed += 1


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
        trajectory_summary=None,
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


def test_generate_falls_back_to_persisted_summary(monkeypatch):
    # No summary passed by the caller (e.g. self-hosted server) -> reuse the
    # trial's own persisted trajectory_summary phases instead of re-segmenting.
    async def _fake_read_trajectory(trial):
        return {"steps": [{"step_id": "1", "source": "agent", "tool_calls": [{"function_name": "bash"}]}]}

    async def _none(trial):
        return None

    monkeypatch.setattr(trial_io, "read_trial_trajectory", _fake_read_trajectory)
    monkeypatch.setattr(trial_io, "read_trial_instruction", _none)
    monkeypatch.setattr(trial_io, "read_trial_verifier_output", _none)

    trial = SimpleNamespace(
        id="trial-3", status="success", reward=1.0, error_message=None,
        name="t", agent="a", trajectory_graph=None,
        trajectory_summary={
            "phases": [{"label": "Explore", "step_ids": [1], "gist": "looked"}],
            "highlights": [],
        },
    )
    session = _FakeSession()
    graph = asyncio.run(
        trial_io.generate_and_store_trajectory_graph(session, trial)
    )
    assert graph["source"] == "summary"
    assert [s["title"] for s in graph["steps"]] == ["Explore"]


def test_generate_returns_cached_when_fresh_and_not_refresh(monkeypatch):
    calls = {"n": 0}

    async def _fake_read_trajectory(trial):
        calls["n"] += 1
        return {"steps": []}

    monkeypatch.setattr(trial_io, "read_trial_trajectory", _fake_read_trajectory)

    fresh = {"schema_version": tg.GRAPH_SCHEMA_VERSION, "steps": [], "terminal": {}}
    trial = SimpleNamespace(
        trajectory_summary=None,
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
