"""Hosted verdict-synthesis: routes task-verdict synthesis through
VerdictBlock + AnalyzerBlock instead of oddish's legacy compute_task_verdict
call.

Registered over the core QA handler (QaJobHandler.verdict_synth_fn) at worker
container load, gated on settings.verdict_via_analyzer_block -- following
analyzer_sandbox.py's shape: gate registration, not handler internals, so
unsetting the flag reverts task QA to the legacy path with no branching.
"""

from __future__ import annotations

import asyncio

from oddish.analyze.classifier import VERDICT_MAX_TOKENS, VERDICT_TIMEOUT
from oddish.analyze.models import (
    BaselineValidation,
    TaskVerdictModel,
    TrialClassification,
)
from oddish.config import settings
from oddish.workers.jobs.handlers import QaJobHandler

from api.services.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from api.services.blocks.analyzer.analyzer_llm_client import (
    LLMClientType,
    OpenAIAnalyzerLLMClient,
)
from api.services.blocks.analyzer.verdict.verdict_block import VerdictBlock


async def verdict_block_synth(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None,
    quality_check_passed: bool,
    timeout: float,
) -> TaskVerdictModel:
    """VerdictSynthFn implementation backed by VerdictBlock/AnalyzerBlock.

    Self-provisions an OpenAIAnalyzerLLMClient for ``settings.verdict_model``
    with TaskVerdictModel as the response format and VERDICT_MAX_TOKENS as the
    token cap -- the same model and token cap the legacy path reaches (legacy
    resolves the model the same way, via ``settings.verdict_model`` in
    ``qa_handler.default_verdict_synth``, so an env override can't split the
    two paths). AnalyzerBlock never closes an injected client, so it is
    closed here in a finally. ``timeout`` bounds the whole block run (the
    OpenAI client itself has no per-request timeout knob yet), mirroring the
    legacy path's ``timeout or VERDICT_TIMEOUT`` fallback.
    """
    model = settings.verdict_model
    vb = VerdictBlock(
        classifications, baseline=baseline, quality_check_passed=quality_check_passed
    )
    client = OpenAIAnalyzerLLMClient(
        model=model,
        max_tokens=VERDICT_MAX_TOKENS,
        response_format=TaskVerdictModel,
    )
    try:
        # vb.build_prompt() can now raise (see VerdictBlock.build_prompt) if
        # its section degraded to the fallback sentinel -- keep it inside
        # this try so the already-constructed client still gets closed.
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.TASK_VERDICT,
            llm_client_type=LLMClientType.OPENAI,
            input=AnalyzerInput(input={"num_trials": len(classifications)}),
            prompt=vb.build_prompt(),
            model=model,
            output_transform=vb.to_verdict,
            client=client,
        )
        out = await asyncio.wait_for(block.run(), timeout=timeout or VERDICT_TIMEOUT)
    finally:
        await client.aclose()
    return TaskVerdictModel(**out.output)


class VerdictBlockQaJobHandler(QaJobHandler):
    """Same QA orchestration as QaJobHandler -- only the verdict-synthesis
    strategy differs."""

    verdict_synth_fn = staticmethod(verdict_block_synth)


def install_verdict_block_qa_handler() -> bool:
    """Swap the core QA handler for the AnalyzerBlock-backed one. Returns
    whether it was installed, so the caller can log the mode."""
    from oddish.workers.jobs import register

    if not settings.verdict_via_analyzer_block:
        return False
    register(VerdictBlockQaJobHandler(), override=True)
    return True
