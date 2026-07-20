"""The verdict-synthesis strategy is injectable so the hosted backend can swap
in an AnalyzerBlock-backed implementation without forking QaJobHandler.run's
task-status / retry bookkeeping. Mirrors test_analyzer_eval_injection.py's
eval_rows_fn coverage for AnalyzerJobHandler."""

import pytest

from oddish.workers.jobs.handlers import QaJobHandler
from oddish.workers.queue import qa_handler as qh

pytestmark = pytest.mark.asyncio


async def _other_strategy(classifications, baseline, quality_check_passed, timeout):
    return "OTHER"


async def test_handler_default_verdict_synth_is_the_default_strategy():
    assert QaJobHandler.verdict_synth_fn is qh.default_verdict_synth


async def test_subclass_can_swap_verdict_synth_strategy():
    class _Sub(QaJobHandler):
        verdict_synth_fn = staticmethod(_other_strategy)

    assert _Sub.verdict_synth_fn is _other_strategy
    assert QaJobHandler.verdict_synth_fn is qh.default_verdict_synth  # not clobbered


async def test_instance_access_does_not_bind_the_strategy_as_a_method():
    """staticmethod matters only on instance access, which is how run() calls it.
    Without the wrapper this returns a bound method and self is silently
    prepended."""
    assert QaJobHandler().verdict_synth_fn is qh.default_verdict_synth

    class _Sub(QaJobHandler):
        verdict_synth_fn = staticmethod(_other_strategy)

    assert _Sub().verdict_synth_fn is _other_strategy
