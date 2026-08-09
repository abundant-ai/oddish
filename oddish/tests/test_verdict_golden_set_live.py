"""Opt-in live backtest: the REAL verdict model judges the golden set.

Skipped unless the credentials for ``settings.verdict_model``'s configured
OpenAI provider resolve (the same check ``_build_openai_client`` performs),
so normal CI never touches the network. Run it with output visible:

    uv run pytest tests/test_verdict_golden_set_live.py -s

To compare two prompt versions, point ``ODDISH_VERDICT_PROMPT_FILE`` at an
alternative prompt file and re-run; the run patches the loaded prompt text
(``classifier._VERDICT_PROMPT``), which is what ``build_verdict_prompt``
formats, so no production code changes are needed.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from oddish.analyze import classifier
from oddish.analyze.classifier import VERDICT_TIMEOUT
from oddish.config import OPENAI_PROVIDER_OPENAI, settings
from verdict_golden_set import GOLDEN_CASES


def _verdict_credentials_resolve() -> bool:
    try:
        if settings.get_openai_provider() == OPENAI_PROVIDER_OPENAI:
            settings.require_public_openai_config()
        else:
            settings.require_azure_openai_config()
    except Exception:
        return False
    return True


requires_verdict_model = pytest.mark.skipif(
    not _verdict_credentials_resolve(),
    reason="no credentials for the configured OpenAI provider; "
    "the live verdict backtest is opt-in",
)


@requires_verdict_model
@pytest.mark.asyncio
async def test_live_backtest_over_golden_set(monkeypatch):
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock
    from oddish.workers.queue.qa_handler import synthesize_task_verdict

    # The backtest measures the model, not persistence: no S3/Postgres needed.
    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", AsyncMock())
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", AsyncMock())
    monkeypatch.setattr(AnalyzerBlock, "record_cost", AsyncMock())

    override = os.environ.get("ODDISH_VERDICT_PROMPT_FILE")
    if override:
        monkeypatch.setattr(classifier, "_VERDICT_PROMPT", Path(override).read_text())

    rows = []
    for case in GOLDEN_CASES:
        try:
            verdict = await synthesize_task_verdict(
                case.classifications,
                case.baseline,
                case.quality_check_passed,
                VERDICT_TIMEOUT,
                pre_trial_items=case.pre_trial_items,
            )
            got, confidence = verdict.verdict, verdict.confidence
        except Exception as exc:  # noqa: BLE001 - one dead case must not hide the table
            got, confidence = f"error({type(exc).__name__})", "-"
        rows.append((case.name, case.expected_verdict, got, confidence))

    width = max(len(r[0]) for r in rows)
    header = f"{'case':<{width}}  {'expected':<8}  {'got':<8}  confidence"
    print(
        f"\nverdict model: {settings.verdict_model}"
        + (f"  prompt: {override}" if override else "")
    )
    print(header)
    print("-" * len(header))
    for name, expected, got, confidence in rows:
        flag = "" if got == expected else "  <-- MISMATCH"
        print(f"{name:<{width}}  {expected:<8}  {got:<8}  {confidence}{flag}")
    accepts = sum(1 for r in rows if r[2] == "accept")
    print(f"accept rate: {accepts}/{len(rows)}")

    mismatches = [r for r in rows if r[2] != r[1]]
    assert not mismatches, f"{len(mismatches)} golden case(s) drifted: " + ", ".join(
        f"{n} expected {e} got {g}" for n, e, g, _ in mismatches
    )
