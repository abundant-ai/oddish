"""The eval strategy is injectable so the hosted backend can swap in a sandbox
implementation without forking run_analyzer_generation_job's reap-safety code."""

import pytest

from oddish.evals.analyzer.schemas import AnalyzerEvalConfig, AnalyzerEvalOutput
from oddish.workers.jobs.handlers import AnalyzerJobHandler
from oddish.workers.queue import analyzer_handler as ah

pytestmark = pytest.mark.asyncio


async def test_default_eval_rows_builds_inputs_then_runs_core(monkeypatch):
    seen = {}

    async def fake_build(rows):
        seen["rows"] = rows
        return "INPUTS"

    async def fake_run(inputs, config):
        seen["inputs"] = inputs
        seen["config"] = config
        return AnalyzerEvalOutput(sections={})

    monkeypatch.setattr(ah, "build_analyzer_inputs", fake_build)
    monkeypatch.setattr(ah, "run_analyzer_eval", fake_run)

    config = AnalyzerEvalConfig()
    out = await ah.default_eval_rows([("trial", "task/path")], config, "a1")

    assert seen["rows"] == [("trial", "task/path")]
    assert seen["inputs"] == "INPUTS"
    assert seen["config"] is config
    assert isinstance(out, AnalyzerEvalOutput)


async def test_handler_default_eval_rows_is_the_default_strategy():
    assert AnalyzerJobHandler.eval_rows_fn is ah.default_eval_rows


async def test_subclass_can_swap_eval_strategy():
    async def other(rows, config, analyzer_id):
        return AnalyzerEvalOutput(sections={})

    class _Sub(AnalyzerJobHandler):
        eval_rows_fn = staticmethod(other)

    assert _Sub.eval_rows_fn is other
    assert AnalyzerJobHandler.eval_rows_fn is ah.default_eval_rows  # not clobbered
