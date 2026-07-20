"""Parity test: the legacy verdict-synth path (default_verdict_synth, backed
by oddish.analyze.compute_task_verdict) and the AnalyzerBlock-backed path
(verdict_block_synth) must produce byte-identical build_verdict_payload
output from the same classifications.

The fixture's four counts are mutually distinguishable (1/2/3/4, derived from
four disjoint Classification buckets) and the legacy-side TaskVerdict carries
garbage (999) counts that build_verdict_payload must recompute away -- a
fixture where every count is 1 (or where the counts aren't actually exercised)
would pass even if build_verdict_payload trusted the model's counts.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from oddish.analyze.models import Classification, TaskVerdict, TrialClassification
from oddish.core.verdict_sync import build_verdict_payload


def _classification(name, classification, **over):
    base = dict(
        trial_name=name,
        classification=classification,
        subtype="subtype",
        evidence="evidence",
        root_cause="root cause",
        recommendation="N/A",
        reward=1.0,
    )
    base.update(over)
    return TrialClassification(**base)


def _fixture_classifications() -> list[TrialClassification]:
    return [
        # task_problem_count -> 1
        _classification("bad-failure-1", Classification.BAD_FAILURE, reward=0.0),
        # agent_problem_count -> 2
        _classification("good-failure-1", Classification.GOOD_FAILURE, reward=0.0),
        _classification("good-failure-2", Classification.GOOD_FAILURE, reward=0.0),
        # success_count -> 3
        _classification("good-success-1", Classification.GOOD_SUCCESS),
        _classification("good-success-2", Classification.GOOD_SUCCESS),
        _classification("good-success-3", Classification.GOOD_SUCCESS),
        # harness_error_count -> 4
        _classification("harness-1", Classification.HARNESS_ERROR, reward=None),
        _classification("harness-2", Classification.HARNESS_ERROR, reward=None),
        _classification("harness-3", Classification.HARNESS_ERROR, reward=None),
        _classification("harness-4", Classification.HARNESS_ERROR, reward=None),
    ]


_EXPECTED_COUNTS = {
    "task_problem_count": 1,
    "agent_problem_count": 2,
    "success_count": 3,
    "harness_error_count": 4,
}

_JUDGMENT = {
    "is_good": False,
    "confidence": "medium",
    "primary_issue": "flaky verifier",
    "reasoning": "several trials disagree on outcome",
    "recommendations": ["tighten the verifier", "add a regression trial"],
}


def _patch_legacy(monkeypatch):
    """Legacy path: oddish.analyze.compute_task_verdict returns a TaskVerdict
    whose counts are garbage (999) -- build_verdict_payload must recompute
    them from classifications rather than trust the model's object."""

    def _fake_compute_task_verdict(
        classifications,
        baseline=None,
        quality_check_passed=True,
        model=None,
        console=None,
        verbose=False,
        api_key=None,
        timeout=None,
    ):
        return TaskVerdict(
            is_good=_JUDGMENT["is_good"],
            confidence=_JUDGMENT["confidence"],
            primary_issue=_JUDGMENT["primary_issue"],
            reasoning=_JUDGMENT["reasoning"],
            recommendations=list(_JUDGMENT["recommendations"]),
            task_problem_count=999,
            agent_problem_count=999,
            success_count=999,
            harness_error_count=999,
            classifications=classifications,
            baseline=baseline,
        )

    monkeypatch.setattr(
        "oddish.analyze.compute_task_verdict", _fake_compute_task_verdict
    )


# --- block-path OpenAI stream fake (see test_verdict_block.py for the same
# shape; duplicated here to keep the parity test file self-contained) -------


class _FakeStreamEvent:
    def __init__(self, type_, delta=None):
        self.type = type_
        self.delta = delta


class _FakeOpenAIStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def __aiter__(self):
        for event in self._events:
            yield event


class _FakeOpenAICompletions:
    def __init__(self, events):
        self._events = events

    def stream(self, **kwargs):
        return _FakeOpenAIStream(self._events)


class _FakeOpenAIChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeAsyncOpenAI:
    def __init__(self, events):
        self.chat = _FakeOpenAIChat(_FakeOpenAICompletions(events))

    async def close(self):
        pass


def _patch_block(monkeypatch):
    """Block path: the OpenAI stream returns the SAME judgment fields as the
    legacy fake. TaskVerdictModel has no count fields at all -- the block
    path structurally cannot carry a garbage count."""
    from api.services.blocks.analyzer.analyzer_block import AnalyzerBlock

    body = json.dumps(_JUDGMENT)
    events = [_FakeStreamEvent("content.delta", delta=body)]
    fake = _FakeAsyncOpenAI(events)

    def _builder(*, model, api_key=None):
        return fake, model

    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client._build_openai_client",
        _builder,
    )
    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", AsyncMock())
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", AsyncMock())


@pytest.mark.asyncio
async def test_legacy_and_block_synth_produce_identical_verdict_payload(monkeypatch):
    from oddish.workers.queue.qa_handler import default_verdict_synth
    from worker.verdict_synth import verdict_block_synth

    classifications = _fixture_classifications()

    _patch_legacy(monkeypatch)
    legacy_verdict = await default_verdict_synth(classifications, None, True, 180)
    legacy_payload = build_verdict_payload(legacy_verdict, classifications)

    _patch_block(monkeypatch)
    block_verdict = await verdict_block_synth(classifications, None, True, 180)
    block_payload = build_verdict_payload(block_verdict, classifications)

    assert legacy_payload == block_payload

    for payload in (legacy_payload, block_payload):
        for key, expected in _EXPECTED_COUNTS.items():
            assert payload[key] == expected, (
                f"{key} should be recomputed to {expected}, got {payload[key]} "
                "(a garbage/mismatched count means build_verdict_payload trusted "
                "the model instead of recomputing from classifications)"
            )
        assert payload["is_good"] == _JUDGMENT["is_good"]
        assert payload["confidence"] == _JUDGMENT["confidence"]
        assert payload["primary_issue"] == _JUDGMENT["primary_issue"]
        assert payload["reasoning"] == _JUDGMENT["reasoning"]
        assert payload["recommendations"] == _JUDGMENT["recommendations"]
