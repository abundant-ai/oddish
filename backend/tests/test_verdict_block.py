"""Tests for the VerdictBlock prompt/parse block and the backend
verdict-synth wiring (worker.verdict_synth.verdict_block_synth)."""

from __future__ import annotations

import json

import pytest

from api.services.blocks.analyzer.verdict.verdict_block import VerdictBlock
from oddish.analyze import build_verdict_prompt
from oddish.analyze.models import (
    BaselineResult,
    BaselineValidation,
    Classification,
    TrialClassification,
)


def _classification(name, classification, **over):
    base = dict(
        trial_name=name,
        classification=classification,
        subtype="Correct Solution",
        evidence="evidence",
        root_cause="root cause",
        recommendation="N/A",
        reward=1.0,
    )
    base.update(over)
    return TrialClassification(**base)


def _classifications():
    return [
        _classification("t1", Classification.GOOD_SUCCESS),
        _classification("t2", Classification.BAD_FAILURE, reward=0.0),
    ]


# ---------------------------------------------------------------------------
# VerdictBlock: prompt parity + parse/to_verdict
# ---------------------------------------------------------------------------


def test_build_prompt_matches_shared_prompt_builder():
    classifications = _classifications()
    baseline = BaselineValidation(
        nop=BaselineResult(agent="nop", passed=False, reward=0.0)
    )
    vb = VerdictBlock(classifications, baseline=baseline, quality_check_passed=False)
    assert vb.build_prompt() == build_verdict_prompt(classifications, baseline, False)


def test_build_prompt_matches_shared_prompt_builder_with_defaults():
    classifications = _classifications()
    vb = VerdictBlock(classifications)
    assert vb.build_prompt() == build_verdict_prompt(classifications, None, True)


def test_parse_produces_task_verdict_model():
    vb = VerdictBlock(_classifications())
    raw = json.dumps(
        {
            "is_good": True,
            "confidence": "high",
            "primary_issue": None,
            "recommendations": [],
            "reasoning": "fine",
        }
    )
    out = vb.parse(raw)
    assert out.is_good is True
    assert out.confidence == "high"
    assert out.primary_issue is None
    assert out.recommendations == []
    assert out.reasoning == "fine"


def test_to_verdict_returns_plain_dict():
    vb = VerdictBlock(_classifications())
    raw = json.dumps(
        {
            "is_good": False,
            "confidence": "low",
            "primary_issue": "bad tests",
            "recommendations": ["fix x"],
            "reasoning": None,
        }
    )
    out = vb.to_verdict(raw)
    assert out == {
        "is_good": False,
        "confidence": "low",
        "primary_issue": "bad tests",
        "recommendations": ["fix x"],
        "reasoning": None,
    }
    assert isinstance(out, dict)


def test_to_verdict_strips_code_fences():
    vb = VerdictBlock(_classifications())
    body = json.dumps({"is_good": True, "confidence": "medium", "recommendations": []})
    out = vb.to_verdict(f"```json\n{body}\n```")
    assert out["is_good"] is True
    assert out["confidence"] == "medium"


def test_parse_raises_block_parse_error_on_bad_json():
    from api.services.blocks.block import BlockParseError

    vb = VerdictBlock(_classifications())
    with pytest.raises(BlockParseError):
        vb.parse("not json")


# ---------------------------------------------------------------------------
# worker.verdict_synth.verdict_block_synth: production construction wiring
#
# These patch only _build_openai_client (analyzer_llm_client's module-level
# seam), not OpenAIAnalyzerLLMClient itself or AnalyzerBlock -- so the real
# construction path in verdict_block_synth is exercised, not a test double
# injected in its place. That is what catches a dropped response_format /
# max_tokens / output_transform that an all-injected-client test suite would
# miss (see the plan's "Prove your guards" note).
# ---------------------------------------------------------------------------


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
    def __init__(self, events, sent):
        self._events = events
        self._sent = sent

    def stream(self, **kwargs):
        self._sent.update(kwargs)
        return _FakeOpenAIStream(self._events)


class _FakeOpenAIChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeAsyncOpenAI:
    def __init__(self, events, sent):
        self.chat = _FakeOpenAIChat(_FakeOpenAICompletions(events, sent))
        self.closed = False

    async def close(self):
        self.closed = True


def _patch_openai_builder(monkeypatch, payload: dict, *, runtime_model="gpt-5.4"):
    """Bypass ``_build_openai_client`` so constructing the client never needs
    live OpenAI/Azure credentials, and stream back ``payload`` as a single
    content.delta chunk."""
    body = json.dumps(payload)
    events = [_FakeStreamEvent("content.delta", delta=body)]
    sent: dict = {}
    fake = _FakeAsyncOpenAI(events, sent)

    def _builder(*, model, api_key=None):
        return fake, runtime_model

    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_llm_client._build_openai_client",
        _builder,
    )
    return fake, sent


def _patch_block_persistence(monkeypatch):
    from unittest.mock import AsyncMock

    from api.services.blocks.analyzer.analyzer_block import AnalyzerBlock

    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", AsyncMock())
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", AsyncMock())


@pytest.mark.asyncio
async def test_verdict_block_synth_default_construction_wires_response_format_and_max_tokens(
    monkeypatch,
):
    from oddish.analyze.classifier import VERDICT_MAX_TOKENS
    from oddish.analyze.models import TaskVerdictModel
    from worker.verdict_synth import verdict_block_synth

    _patch_block_persistence(monkeypatch)
    payload = {
        "is_good": True,
        "confidence": "high",
        "primary_issue": None,
        "recommendations": [],
        "reasoning": "ok",
    }
    fake, sent = _patch_openai_builder(monkeypatch, payload)

    result = await verdict_block_synth(_classifications(), None, True, 180)

    assert sent["response_format"] is TaskVerdictModel
    assert sent["max_tokens"] == VERDICT_MAX_TOKENS
    assert fake.closed is True
    assert result.is_good is True
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_verdict_block_synth_uses_task_verdict_analyzer_type_and_real_output_transform(
    monkeypatch,
):
    from api.services.blocks.analyzer.analyzer_block import AnalyzerBlock, AnalyzerType
    from worker.verdict_synth import verdict_block_synth

    _patch_block_persistence(monkeypatch)
    payload = {"is_good": True, "confidence": "high", "recommendations": []}
    _patch_openai_builder(monkeypatch, payload)

    captured = {}
    real_init = AnalyzerBlock.__init__

    def _spy_init(self, **kwargs):
        captured.update(kwargs)
        real_init(self, **kwargs)

    monkeypatch.setattr(AnalyzerBlock, "__init__", _spy_init)

    await verdict_block_synth(_classifications(), None, True, 180)

    assert captured["analyzer_type"] is AnalyzerType.TASK_VERDICT

    # output_transform must be the real VerdictBlock.to_verdict, not just any
    # non-None callable: prove it actually parses per VerdictBlock's contract.
    ot = captured["output_transform"]
    out = ot(
        json.dumps({"is_good": True, "confidence": "medium", "recommendations": []})
    )
    assert out == {
        "is_good": True,
        "confidence": "medium",
        "primary_issue": None,
        "recommendations": [],
        "reasoning": None,
    }


@pytest.mark.asyncio
async def test_verdict_block_synth_closes_the_client_it_creates(monkeypatch):
    from worker.verdict_synth import verdict_block_synth

    _patch_block_persistence(monkeypatch)
    payload = {"is_good": False, "confidence": "low", "recommendations": []}
    fake, _sent = _patch_openai_builder(monkeypatch, payload)

    await verdict_block_synth(_classifications(), None, True, 180)

    assert fake.closed is True
