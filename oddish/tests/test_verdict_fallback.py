"""Verdict synthesis falls back to a Claude model when the OpenAI/Azure
provider returns a permanent error.

Motivating incident: Azure content-policy blocked the resource behind
``settings.verdict_model`` on 2026-07-26 and every task verdict in prod died
for 58 hours, while post-trial/pre-trial (Claude) kept succeeding in the same
worker.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oddish.analyze.models import Classification, TrialClassification
from oddish.config import settings
from oddish.workers.queue import qa_handler


AZURE_403 = (
    "Error code: 403 - {'error': {'message': 'Your resource has been "
    "temporarily blocked because we detected behavior that may violate our "
    "content policy.'}}"
)


class PermissionDeniedError(Exception):
    """Stand-in for the OpenAI SDK error; classification is name/text based."""


def _classifications() -> list[TrialClassification]:
    return [
        TrialClassification(
            trial_name="t1",
            classification=Classification.GOOD_FAILURE,
            subtype="gave_up",
            evidence="e",
            root_cause="rc",
            recommendation="r",
            reward=0.0,
        )
    ]


def _install_fake_block(monkeypatch, *, behaviors):
    """Patch AnalyzerBlock so each construction pops one behavior.

    ``behaviors`` entries are either an exception to raise from ``run()`` or a
    dict to return as the block output. Returns the list of constructor kwargs
    seen, so tests can assert which model/schema each attempt used.
    """
    seen: list[dict] = []
    queue = list(behaviors)

    class _FakeBlock:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            self._behavior = queue.pop(0)

        async def run(self):
            if isinstance(self._behavior, Exception):
                raise self._behavior
            return SimpleNamespace(output=self._behavior)

    import oddish.blocks.analyzer.analyzer_block as ab

    monkeypatch.setattr(ab, "AnalyzerBlock", _FakeBlock)
    return seen


GOOD_OUTPUT = {
    "is_good": True,
    "confidence": "high",
    "primary_issue": None,
    "recommendations": [],
    "reasoning": "fine",
}


@pytest.mark.asyncio
async def test_permanent_provider_error_falls_back_to_claude(monkeypatch):
    seen = _install_fake_block(
        monkeypatch,
        behaviors=[PermissionDeniedError(AZURE_403), GOOD_OUTPUT],
    )

    verdict = await qa_handler.synthesize_task_verdict(
        _classifications(), None, True, 30.0, task_id="task-1"
    )

    assert verdict.is_good is True
    assert len(seen) == 2, "expected one primary attempt and one fallback"
    assert seen[0]["model"] == settings.verdict_model
    assert seen[1]["model"] == settings.verdict_fallback_model


@pytest.mark.asyncio
async def test_fallback_sends_a_json_schema_not_response_format(monkeypatch):
    """The Anthropic path ignores ``response_format``; without an
    ``output_schema`` the fallback would generate unconstrained free text and
    the verdict parse would fail."""
    seen = _install_fake_block(
        monkeypatch,
        behaviors=[PermissionDeniedError(AZURE_403), GOOD_OUTPUT],
    )

    await qa_handler.synthesize_task_verdict(
        _classifications(), None, True, 30.0, task_id="task-1"
    )

    fallback = seen[1]
    assert fallback["output_schema"] is not None
    assert fallback["output_schema"]["properties"]["is_good"]
    assert fallback["response_format"] is None


@pytest.mark.asyncio
async def test_transient_error_does_not_fall_back(monkeypatch):
    """A timeout must keep the worker-job retry semantics it has today rather
    than silently switching models."""
    seen = _install_fake_block(monkeypatch, behaviors=[TimeoutError(), GOOD_OUTPUT])

    with pytest.raises(TimeoutError):
        await qa_handler.synthesize_task_verdict(
            _classifications(), None, True, 30.0, task_id="task-1"
        )

    assert len(seen) == 1, "transient failures must not consume the fallback"


@pytest.mark.asyncio
async def test_fallback_failure_propagates(monkeypatch):
    seen = _install_fake_block(
        monkeypatch,
        behaviors=[PermissionDeniedError(AZURE_403), RuntimeError("claude down")],
    )

    with pytest.raises(RuntimeError, match="claude down"):
        await qa_handler.synthesize_task_verdict(
            _classifications(), None, True, 30.0, task_id="task-1"
        )

    assert len(seen) == 2


@pytest.mark.asyncio
async def test_output_schema_reaches_the_api_client():
    """The fallback is only structured if ``output_schema`` survives
    AnalyzerBlock -> create_llm_client -> ApiAnalyzerLLMClient. Dropping it
    anywhere in that chain leaves the Anthropic path generating free text."""
    from oddish.blocks.analyzer.analyzer_llm_client import (
        LLMClientType,
        create_llm_client,
    )

    schema = {"type": "object", "properties": {"is_good": {"type": "boolean"}}}
    client = await create_llm_client(
        LLMClientType.API,
        model=settings.verdict_fallback_model,
        output_schema=schema,
    )

    assert client._output_schema == schema
    # The fallback model must route to the Anthropic path, or output_schema is
    # ignored and response_format (which we deliberately do not send) would be
    # the only structuring.
    assert client._uses_openai is False


@pytest.mark.asyncio
async def test_primary_success_never_builds_a_second_block(monkeypatch):
    seen = _install_fake_block(monkeypatch, behaviors=[GOOD_OUTPUT])

    verdict = await qa_handler.synthesize_task_verdict(
        _classifications(), None, True, 30.0, task_id="task-1"
    )

    assert verdict.is_good is True
    assert len(seen) == 1
