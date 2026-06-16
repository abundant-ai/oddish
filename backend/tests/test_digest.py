"""Unit tests for the deterministic parts of digest generation."""

import sys
import types

import pytest

from oddish.core import digest as digest_mod
from oddish.core.digest import (
    DigestResult,
    build_digest_prompt,
    generate_digest,
    parse_digest_response,
)


def test_build_digest_prompt_includes_title_and_text():
    p = build_digest_prompt(
        title="Onboarding QA", text="Q: how to reset? A: click reset."
    )
    assert "Onboarding QA" in p
    assert "click reset" in p
    assert "summary" in p.lower() and "tags" in p.lower()


def test_parse_digest_response_plain_json():
    raw = '{"summary": "s", "digest_text": "d", "tags": ["a", "b"]}'
    r = parse_digest_response(raw)
    assert r == DigestResult(summary="s", digest_text="d", tags=["a", "b"])


def test_parse_digest_response_strips_code_fence():
    raw = '```json\n{"summary":"s","digest_text":"d","tags":[]}\n```'
    r = parse_digest_response(raw)
    assert r.summary == "s" and r.tags == []


def test_parse_digest_response_coerces_bad_tags():
    raw = '{"summary":"s","digest_text":"d","tags":"not-a-list"}'
    r = parse_digest_response(raw)
    assert r.tags == []


def test_parse_digest_response_caps_tags_at_eight():
    raw = '{"summary":"s","digest_text":"d","tags":["1","2","3","4","5","6","7","8","9","10"]}'
    r = parse_digest_response(raw)
    assert len(r.tags) == 8


class _FakeMessages:
    def __init__(self, recorder):
        self._recorder = recorder

    async def create(self, *, model, max_tokens, messages):
        self._recorder["model"] = model
        return types.SimpleNamespace(
            content=[
                types.SimpleNamespace(
                    text='{"summary":"s","digest_text":"d","tags":["t"]}'
                )
            ]
        )


class _FakeAnthropic:
    def __init__(self, *args, **kwargs):
        self.messages = _FakeMessages(_FakeAnthropic.recorder)

    recorder: dict = {}


@pytest.mark.asyncio
async def test_generate_digest_uses_direct_api_with_plain_model_id(monkeypatch):
    """Internal digest call must hit the direct Anthropic API with a plain model
    id — NOT AsyncAnthropicBedrock, which has no usable credential in prod and
    500s every upload. Regression for the doc-store ingest failure."""
    _FakeAnthropic.recorder = {}
    fake_anthropic_mod = types.ModuleType("anthropic")
    fake_anthropic_mod.AsyncAnthropic = _FakeAnthropic

    def _no_bedrock(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("digest must not construct AsyncAnthropicBedrock")

    fake_anthropic_mod.AsyncAnthropicBedrock = _no_bedrock
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_mod)

    # Default model is the Bedrock-mappable "claude-sonnet-4-6"; the digest must
    # send the plain API id, never a "global.anthropic.*" inference-profile id.
    result = await generate_digest(title="T", text="hello world")

    assert result == DigestResult(summary="s", digest_text="d", tags=["t"])
    sent = _FakeAnthropic.recorder["model"]
    assert sent == digest_mod.DEFAULT_DIGEST_MODEL == "claude-sonnet-4-6"
    assert not sent.startswith("global.")
    assert ".anthropic." not in sent
