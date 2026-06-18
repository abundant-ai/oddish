"""Tests for ``worker.probe_analysis.run_probe_analyzer``.

The analyzer makes a single Claude API call and parses a JSON response. It runs
on the direct Anthropic API (``ANTHROPIC_API_KEY``) in every environment, so we
patch ``anthropic.AsyncAnthropic`` and assert no Bedrock client is built.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oddish.worker.probe_analysis import (
    _build_envelope_schema,
    _normalize_probe_summary,
    run_probe_analyzer,
)


def _schema_fake_client():
    fake_response = MagicMock()
    fake_response.content = [
        MagicMock(
            text=json.dumps(
                {
                    "headline": "h",
                    "summary": "s",
                    "key_actions": [],
                    "cheating_attempted": None,
                    "cheating_succeeded": None,
                    "evidence": "e",
                    "hypotheses": [],
                    "recommendations": [],
                    "result_focus_findings": {"verdict": "bad", "defects": ["x"]},
                    "attempts": [],
                }
            )
        )
    ]
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)
    return fake_client


SCHEMA = '{"type":"object","properties":{"verdict":{"enum":["good","bad"]},"defects":{"type":"array","items":{"type":"string"}}},"required":["verdict","defects"]}'


@pytest.mark.asyncio
async def test_schema_mode_returns_object_findings_and_enforces():
    fake = _schema_fake_client()
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await run_probe_analyzer(
            extra_instructions="audit",
            agent_messages=[{"kind": "assistant_text", "text": "hi"}],
            verifier_stdout="",
            reward=0.0,
            result_focus=SCHEMA,
        )
    # findings is a real dict, not a stringified blob
    assert result["result_focus_findings"] == {"verdict": "bad", "defects": ["x"]}
    assert result["result_focus_question"] is None
    # structured output was enforced
    _, kwargs = fake.messages.create.call_args
    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert (
        fmt["schema"]["properties"]["result_focus_findings"]["properties"]["verdict"]
        == {"enum": ["good", "bad"]}
    )


def test_normalize_passes_object_findings_through():
    out = _normalize_probe_summary(
        {"result_focus_findings": {"a": 1}}, result_focus="{}", model="m"
    )
    assert out["result_focus_findings"] == {"a": 1}
    assert out["result_focus_question"] is None


def test_normalize_prose_keeps_string_and_question():
    out = _normalize_probe_summary(
        {"result_focus_findings": "two ambiguities"},
        result_focus="Any ambiguities?",
        model="m",
    )
    assert out["result_focus_findings"] == "two ambiguities"
    assert out["result_focus_question"] == "Any ambiguities?"


def test_build_envelope_nests_findings_schema():
    env = _build_envelope_schema({"type": "object", "properties": {}})
    assert env["additionalProperties"] is False
    assert env["properties"]["result_focus_findings"]["type"] == "object"


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
            agent_messages=[{"kind": "assistant_text", "text": "Looking at tests..."}],
            verifier_stdout="passed: 0",
            reward=0.0,
            result_focus="Did the agent find spec ambiguities?",
        )

    assert result["kind"] == "probe_summary"
    assert result["headline"] == "agent enumerated cheats"
    assert result["cheating_attempted"] is False
    assert result["cheating_succeeded"] is None
    assert result["model"] == "claude-sonnet-4-6"
    assert isinstance(result["attempts"], list)
    assert result["attempts"][0]["title"] == "hardcode results.json"
    assert result["attempts"][0]["step_indices"] == [1, 2, 3]
    assert result["attempts"][0]["success"] is False
    assert result["result_focus_findings"] == "found two ambiguities"
    assert result["result_focus_question"] == "Did the agent find spec ambiguities?"
    # prose-mode result_focus must NOT trigger structured output enforcement
    assert "output_config" not in fake_client.messages.create.await_args.kwargs


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
            model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        )

    direct.assert_called_once()
    bedrock.assert_not_called()
    # The Bedrock inference-profile id is resolved to its plain API id, both for
    # the actual API call and for the model recorded on the summary.
    assert result["model"] == "claude-haiku-4-5"
    assert fake_client.messages.create.await_args.kwargs["model"] == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# _normalize_probe_summary — recommendations field
# ---------------------------------------------------------------------------


def _normalize(parsed: dict) -> dict:
    return _normalize_probe_summary(parsed, result_focus="", model="m")


def test_normalize_recommendations_passthrough():
    out = _normalize(
        {
            "recommendations": [
                {
                    "priority": "must_fix",
                    "action": "Compute results from source",
                    "rationale": "A pre-written results.json passes outright",
                },
                {"priority": "optional", "action": "Remove /opt/reference"},
            ]
        }
    )
    assert out["recommendations"] == [
        {
            "priority": "must_fix",
            "action": "Compute results from source",
            "rationale": "A pre-written results.json passes outright",
        },
        {
            "priority": "optional",
            "action": "Remove /opt/reference",
            "rationale": "",
        },
    ]


def test_normalize_recommendations_bad_priority_coerced():
    out = _normalize({"recommendations": [{"priority": "nope", "action": "do thing"}]})
    assert out["recommendations"][0]["priority"] == "should_fix"


def test_normalize_recommendations_drops_entries_without_action():
    out = _normalize(
        {
            "recommendations": [
                {"priority": "must_fix", "action": "   "},
                {"priority": "must_fix"},
                {"priority": "should_fix", "action": "keep me"},
            ]
        }
    )
    assert out["recommendations"] == [
        {"priority": "should_fix", "action": "keep me", "rationale": ""}
    ]


def test_normalize_recommendations_missing_field_defaults_empty():
    out = _normalize({"headline": "hi"})
    assert out["recommendations"] == []


@pytest.mark.asyncio
async def test_probe_analyzer_prompt_requests_recommendations():
    """The analyzer prompt must instruct the model to emit `recommendations`."""
    fake_client = _fake_client()

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        await run_probe_analyzer(
            extra_instructions="find cheats",
            agent_messages=[{"kind": "assistant_text", "text": "..."}],
            verifier_stdout="",
            reward=0.0,
            result_focus="",
        )

    sent = fake_client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "recommendations" in sent
    assert "must_fix" in sent
    assert "should_fix" in sent
    assert "optional" in sent
