"""Tests for ``worker.probe_analysis.run_probe_analyzer``.

The analyzer makes a single Claude API call and parses a JSON response.
We patch ``probe_analysis._make_client`` so the test runs offline regardless
of how the model id routes (Bedrock vs. direct API); credential/region
routing is covered separately by the ``_make_client`` tests below.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oddish.worker.probe_analysis import run_probe_analyzer


@pytest.mark.asyncio
async def test_probe_analyzer_parses_json_response():
    """Happy path: model returns clean JSON, analyzer fills the dict."""
    fake_response = MagicMock()
    fake_response.content = [
        MagicMock(
            text=(
                '{"headline":"agent enumerated cheats",'
                '"summary":"...",'
                '"key_actions":["read tests"],'
                '"cheating_attempted":false,'
                '"cheating_succeeded":null,'
                '"evidence":"transcript step 4",'
                '"result_focus_findings":"found two ambiguities",'
                '"attempts":[{"title":"hardcode results.json","rationale":"verifier reads it","outcome":"rejected","success":false,"step_indices":[1,2,3]}]'
                "}"
            )
        )
    ]
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    with patch(
        "oddish.worker.probe_analysis._make_client", return_value=fake_client
    ):
        result = await run_probe_analyzer(
            extra_instructions="find cheats",
            agent_messages=[
                {"kind": "assistant_text", "text": "Looking at tests..."}
            ],
            verifier_stdout="passed: 0",
            reward=0.0,
            result_focus="Did the agent find spec ambiguities?",
            evaluation_metric="ratio",
            ratio_unit="cheat",
            ratio_verb="succeeded",
        )

    assert result["kind"] == "probe_summary"
    assert result["headline"] == "agent enumerated cheats"
    assert result["cheating_attempted"] is False
    assert result["cheating_succeeded"] is None
    # run_probe_analyzer resolves the default "claude-sonnet-4-6" to its
    # invokable Bedrock inference-profile id before recording it.
    assert result["model"] == "global.anthropic.claude-sonnet-4-6"
    assert isinstance(result["attempts"], list)
    assert result["attempts"][0]["title"] == "hardcode results.json"
    assert result["attempts"][0]["step_indices"] == [1, 2, 3]
    assert result["attempts"][0]["success"] is False
    assert result["result_focus_findings"] == "found two ambiguities"
    assert result["result_focus_question"] == "Did the agent find spec ambiguities?"


def test_make_client_bedrock_prefers_bearer_token(monkeypatch):
    """Bedrock model id + bearer token -> bearer auth, explicit region.

    Mirrors the claude-code agent's resolution so the inline summary rides the
    same Bedrock auth route that just worked for the agent, instead of letting
    the SDK fall into SigV4 with the ambient S3-scoped creds.
    """
    from oddish.worker import probe_analysis

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "abc123")
    monkeypatch.delenv("AWS_REGION", raising=False)

    captured = {}

    class FakeBedrock:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch("anthropic.AsyncAnthropicBedrock", FakeBedrock):
        probe_analysis._make_client("global.anthropic.claude-haiku-4-5-20251001-v1:0")

    assert captured == {"api_key": "abc123", "aws_region": "us-east-1"}


def test_make_client_bedrock_sigv4_fallback_without_bearer(monkeypatch):
    """Bedrock model id, no bearer token -> SigV4 client, region honored."""
    from oddish.worker import probe_analysis

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    captured = {}

    class FakeBedrock:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch("anthropic.AsyncAnthropicBedrock", FakeBedrock):
        probe_analysis._make_client("global.anthropic.claude-haiku-4-5-20251001-v1:0")

    assert captured == {"aws_region": "us-west-2"}
    assert "api_key" not in captured
