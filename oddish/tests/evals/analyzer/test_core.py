import json
import logging

import pytest

from oddish.evals.primitives import SubAnalysis, TrajectoryBundle, trajectory_link
from oddish.evals.analyzer.core import AnalyzerEvalStepError, run_analyzer_eval
from oddish.evals.analyzer.schemas import AnalyzerEvalConfig, AnalyzerEvalInputs


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
async def test_run_analyzer_eval_maps_per_failure_and_reduces():
    inputs = AnalyzerEvalInputs(
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
    out = await run_analyzer_eval(inputs, AnalyzerEvalConfig(), client=client)

    # counts
    assert out.counts == {"trials": 3, "bad": 1, "good": 1}
    assert out.breakdown["1b"] == 1 and out.breakdown["3a"] == 1
    # exactly one map call per failure trial (2) + one reduce call = 3
    assert len(client.calls) == 3
    # four sections present; links flow through verbatim
    assert set(out.sections) == {"bad", "good", "capabilities", "headroom"}
    assert "/tasks/task/probe/task-0" in out.sections["bad"]
    assert len(out.findings) == 2
    # the reduce prompt that produced the sections is captured on the output,
    # and is exactly the string that was sent to the client (the last call).
    assert out.reduce_prompt == client.calls[-1]
    assert "lead analyst synthesizing" in out.reduce_prompt


@pytest.mark.asyncio
async def test_run_analyzer_eval_no_failures_skips_reduce_llm():
    inputs = AnalyzerEvalInputs(
        bundles=[_bundle("task-9")],
        subanalyses=[_sa("task-9", "GOOD_SUCCESS", "Correct Solution")],
    )
    client = FakeClient()
    out = await run_analyzer_eval(inputs, AnalyzerEvalConfig(), client=client)
    assert out.counts == {"trials": 1, "bad": 0, "good": 0}
    assert client.calls == []  # nothing to analyze → no LLM calls
    assert out.sections["bad"] == "" and out.sections["headroom"] == ""
    # no reduce stage ran, so there is no prompt to persist
    assert out.reduce_prompt is None


def _failure_inputs():
    return AnalyzerEvalInputs(
        bundles=[_bundle("task-0"), _bundle("task-1")],
        subanalyses=[
            _sa("task-0", "BAD_FAILURE", "Hardcoding"),
            _sa("task-1", "GOOD_FAILURE", "Wrong Approach"),
        ],
    )


@pytest.mark.asyncio
async def test_reduce_parse_failure_raises_step_error_with_context():
    """A non-JSON reduce response is fatal and names the step + input in the error."""

    class BadReduceClient(FakeClient):
        async def complete(self, prompt, *, model, temperature, max_tokens):
            if "lead analyst synthesizing" in prompt:  # reduce
                return "sorry, I cannot help with that"  # no JSON object
            return await super().complete(
                prompt, model=model, temperature=temperature, max_tokens=max_tokens
            )

    with pytest.raises(AnalyzerEvalStepError) as ei:
        await run_analyzer_eval(_failure_inputs(), AnalyzerEvalConfig(), client=BadReduceClient())
    assert ei.value.step == "reduce-parse"
    assert "findings=" in ei.value.context  # the input size is captured


@pytest.mark.asyncio
async def test_all_maps_failing_is_fatal_not_blank_success():
    """If every map call fails there are no findings; that must be a fatal
    'map' step error, not a blank-but-SUCCESS analysis."""

    class AllMapsFail(FakeClient):
        async def complete(self, prompt, *, model, temperature, max_tokens):
            if "lead analyst synthesizing" in prompt:  # reduce should never run
                raise AssertionError("reduce must not run when there are no findings")
            raise RuntimeError("map down")

    with pytest.raises(AnalyzerEvalStepError) as ei:
        await run_analyzer_eval(_failure_inputs(), AnalyzerEvalConfig(), client=AllMapsFail())
    assert ei.value.step == "map"
    assert "attempted=2" in ei.value.context


@pytest.mark.asyncio
async def test_map_failure_is_skipped_not_fatal_and_logged(caplog):
    """One map call blowing up must not sink the eval; it's logged with the trial id."""

    class OneMapRaises(FakeClient):
        async def complete(self, prompt, *, model, temperature, max_tokens):
            is_map = "lead analyst synthesizing" not in prompt
            # Identify the *target* trial (before the cohort roster), matching
            # how FakeClient picks it, so the roster mention of task-1 in
            # task-0's prompt doesn't also trip this.
            if is_map and "task-1" in prompt.split("Your cohort")[0]:
                raise RuntimeError("boom on task-1")
            return await super().complete(
                prompt, model=model, temperature=temperature, max_tokens=max_tokens
            )

    with caplog.at_level(logging.WARNING, logger="oddish.evals.analyzer.core"):
        out = await run_analyzer_eval(_failure_inputs(), AnalyzerEvalConfig(), client=OneMapRaises())

    assert len(out.findings) == 1  # task-0 survived, task-1 skipped
    assert out.sections["bad"]  # reduce still ran
    assert any("task-1" in r.getMessage() and "boom on task-1" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_client_init_failure_raises_step_error(monkeypatch):
    """When no client is injected and building the default one fails, we say so."""
    def _boom():
        raise RuntimeError("no ANTHROPIC_API_KEY")

    monkeypatch.setattr("oddish.evals.analyzer.core._default_client", _boom)
    with pytest.raises(AnalyzerEvalStepError) as ei:
        await run_analyzer_eval(_failure_inputs(), AnalyzerEvalConfig(), client=None)
    assert ei.value.step == "client-init"


@pytest.mark.asyncio
async def test_run_analyzer_eval_no_work_is_pure_without_client_or_env(monkeypatch):
    """No injected client + no ANTHROPIC_API_KEY must not raise on the zero-work path."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    inputs = AnalyzerEvalInputs(
        bundles=[_bundle("task-9")],
        subanalyses=[_sa("task-9", "GOOD_SUCCESS", "Correct Solution")],
    )
    out = await run_analyzer_eval(inputs, AnalyzerEvalConfig(), client=None)
    assert out.counts == {"trials": 1, "bad": 0, "good": 0}
    assert all(v == "" for v in out.sections.values())


class _LyingClient(FakeClient):
    """FakeClient, but its map findings assert a bogus model and link."""

    async def complete(self, prompt, *, model, temperature, max_tokens):
        raw = await super().complete(
            prompt, model=model, temperature=temperature, max_tokens=max_tokens
        )
        d = json.loads(raw)
        if "trial_id" in d:  # a map response, not the reduce sections
            d["model"] = "gpt-5-hallucinated"
            d["trajectory_link"] = "https://evil.example/pwned"
        return json.dumps(d)


@pytest.mark.asyncio
async def test_finding_model_and_link_come_from_the_host_not_the_llm():
    """model is a database fact. The LLM gets no say, same as trajectory_link."""
    sa = _sa("task-0", "BAD_FAILURE", "Hardcoding")
    sa.model = "global.anthropic.claude-opus-4-8-v1:0"
    inputs = AnalyzerEvalInputs(bundles=[_bundle("task-0")], subanalyses=[sa])

    out = await run_analyzer_eval(inputs, AnalyzerEvalConfig(), client=_LyingClient())

    f = out.findings[0]
    assert f.model == "global.anthropic.claude-opus-4-8-v1:0"
    assert f.trajectory_link == trajectory_link("task", "task-0")


@pytest.mark.asyncio
async def test_finding_model_none_when_subanalysis_has_none():
    sa = _sa("task-0", "BAD_FAILURE", "Hardcoding")
    sa.model = None
    inputs = AnalyzerEvalInputs(bundles=[_bundle("task-0")], subanalyses=[sa])

    out = await run_analyzer_eval(inputs, AnalyzerEvalConfig(), client=FakeClient())

    assert out.findings[0].model is None
