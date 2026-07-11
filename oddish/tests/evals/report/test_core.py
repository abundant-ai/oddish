import json

import pytest

from oddish.evals.primitives import SubAnalysis, TrajectoryBundle, trajectory_link
from oddish.evals.report.core import run_report_eval
from oddish.evals.report.schemas import ReportEvalConfig, ReportEvalInputs


class FakeClient:
    """Returns a map finding for map prompts, four sections for the reduce prompt."""

    def __init__(self):
        self.calls = []

    async def complete(self, prompt, *, model, temperature, max_tokens):
        self.calls.append(prompt)
        if "lead analyst synthesizing" in prompt:  # reduce prompt
            return json.dumps({
                "bad_failure_content": "bad [t]( /tasks/task/probe/task-0 )",
                "good_failure_content": "good",
                "universal_capabilities_content": "caps",
                "headroom_analysis": "headroom",
            })
        # map prompt: echo a finding for whichever trial this is
        tid = "task-0" if "task-0" in prompt.split("Your cohort")[0] else "task-1"
        bucket = "bad" if "bucket: bad" in prompt else "good"
        return json.dumps({
            "trial_id": tid, "bucket": bucket,
            "subcategory": "1b" if bucket == "bad" else "3a",
            "evidence_quote": "q", "step_indices": [1], "root_cause": "rc",
            "headroom_signal": "hs", "trajectory_link": trajectory_link("task", tid),
        })


def _bundle(tid, oracle=None):
    return TrajectoryBundle(
        trial_id=tid, task_id="task", task_path="tasks/task", agent="cc",
        model="opus", reward=0.0, trajectory=[{"i": 1}], logs={},
        trajectory_summary={"summary": "s"}, oracle_context=oracle,
        trajectory_link=trajectory_link("task", tid),
    )


def _sa(tid, cls_, subtype):
    return SubAnalysis(
        trial_id=tid, trajectory_link=trajectory_link("task", tid),
        classification=cls_, subtype=subtype, evidence="e",
        root_cause="rc", recommendation="rec",
    )


@pytest.mark.asyncio
async def test_run_report_eval_maps_per_failure_and_reduces():
    inputs = ReportEvalInputs(
        bundles=[
            _bundle("task-0", oracle="oracle did y"),
            _bundle("task-1"),
            _bundle("task-2"),  # a GOOD_SUCCESS — counted only, no map call
        ],
        subanalyses=[
            _sa("task-0", "BAD_FAILURE", "Hardcoding"),
            _sa("task-1", "GOOD_FAILURE", "Wrong Approach"),
            _sa("task-2", "GOOD_SUCCESS", "Correct Solution"),
        ],
    )
    client = FakeClient()
    out = await run_report_eval(inputs, ReportEvalConfig(), client=client)

    # counts
    assert out.counts == {"trials": 3, "bad": 1, "good": 1}
    assert out.breakdown["1b"] == 1 and out.breakdown["3a"] == 1
    # exactly one map call per failure trial (2) + one reduce call = 3
    assert len(client.calls) == 3
    # four sections present; links flow through verbatim
    assert set(out.sections) == {"bad", "good", "capabilities", "headroom"}
    assert "/tasks/task/probe/task-0" in out.sections["bad"]
    assert len(out.findings) == 2


@pytest.mark.asyncio
async def test_run_report_eval_no_failures_skips_reduce_llm():
    inputs = ReportEvalInputs(
        bundles=[_bundle("task-9")],
        subanalyses=[_sa("task-9", "GOOD_SUCCESS", "Correct Solution")],
    )
    client = FakeClient()
    out = await run_report_eval(inputs, ReportEvalConfig(), client=client)
    assert out.counts == {"trials": 1, "bad": 0, "good": 0}
    assert client.calls == []  # nothing to analyze → no LLM calls
    assert out.sections["bad"] == "" and out.sections["headroom"] == ""


@pytest.mark.asyncio
async def test_run_report_eval_no_work_is_pure_without_client_or_env(monkeypatch):
    """No injected client + no ANTHROPIC_API_KEY must not raise on the zero-work path."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    inputs = ReportEvalInputs(
        bundles=[_bundle("task-9")],
        subanalyses=[_sa("task-9", "GOOD_SUCCESS", "Correct Solution")],
    )
    out = await run_report_eval(inputs, ReportEvalConfig(), client=None)
    assert out.counts == {"trials": 1, "bad": 0, "good": 0}
    assert all(v == "" for v in out.sections.values())
