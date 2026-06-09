"""Tests for ``worker.probe_analysis.run_probe_analyzer``.

The analyzer makes a single Claude API call and parses a JSON response. It runs
on the direct Anthropic API (``ANTHROPIC_API_KEY``) in every environment, so we
patch ``anthropic.AsyncAnthropic`` and assert no Bedrock client is built.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oddish.worker.probe_analysis import run_probe_analyzer


def _fake_client() -> MagicMock:
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
    return fake_client


@pytest.mark.asyncio
async def test_probe_analyzer_parses_json_response():
    """Happy path: model returns clean JSON, analyzer fills the dict."""
    fake_client = _fake_client()

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
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


@pytest.mark.asyncio
async def test_probe_analyzer_routes_bedrock_model_through_direct_api():
    """A cloud Bedrock model id is normalized to the plain id and run on the
    direct Anthropic API -- never the SigV4-only ``AsyncAnthropicBedrock``."""
    fake_client = _fake_client()

    with (
        patch("anthropic.AsyncAnthropic", return_value=fake_client) as direct,
        patch("anthropic.AsyncAnthropicBedrock") as bedrock,
    ):
        result = await run_probe_analyzer(
            extra_instructions="",
            agent_messages=[{"kind": "assistant_text", "text": "..."}],
            verifier_stdout="",
            reward=1.0,
            result_focus="",
            evaluation_metric="none",
            model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        )

    direct.assert_called_once()
    bedrock.assert_not_called()
    # The Bedrock inference-profile id is resolved to its plain API id, both for
    # the actual API call and for the model recorded on the summary.
    assert result["model"] == "claude-haiku-4-5"
    assert fake_client.messages.create.await_args.kwargs["model"] == "claude-haiku-4-5"
