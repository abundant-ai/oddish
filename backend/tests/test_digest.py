"""Unit tests for the deterministic parts of digest generation."""

import types

import pytest

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


@pytest.mark.asyncio
async def test_generate_digest_uses_direct_api_with_plain_model_id(monkeypatch):
    """Internal digest call must hit the direct Anthropic API (the funnel's
    ``anthropic/`` litellm route) with a plain model id — never a Bedrock
    inference-profile id. Regression for the doc-store ingest failure."""
    import litellm

    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content='{"summary":"s","digest_text":"d","tags":["t"]}'
                    )
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm, "acompletion", fake)

    # Default model is the Bedrock-mappable "claude-sonnet-4-6"; the digest must
    # send the plain API id, never a "global.anthropic.*" inference-profile id.
    result = await generate_digest(title="T", text="hello world")

    assert result == DigestResult(summary="s", digest_text="d", tags=["t"])
    sent = calls[0]["model"]
    assert sent == "anthropic/claude-sonnet-4-6"
    assert not sent.startswith("global.")
    assert ".anthropic." not in sent
