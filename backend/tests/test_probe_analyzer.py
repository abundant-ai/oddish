"""Tests for ``worker.probe_analysis.run_probe_analyzer``.

The analyzer makes a single Claude call through the ``oddish.core.llm`` funnel
(litellm, direct Anthropic API) and parses a JSON response, so we patch
``litellm.acompletion`` and assert on the request it receives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import litellm

from oddish.worker.probe_analysis import (
    _build_envelope_schema,
    _coerce_analyzer_json,
    _normalize_probe_summary,
    run_probe_analyzer,
)


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            prompt_tokens_details=None,
            cache_creation_input_tokens=0,
        ),
    )


def _patch_acompletion(monkeypatch, text: str) -> list[dict]:
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return _fake_response(text)

    monkeypatch.setattr(litellm, "acompletion", fake)
    return calls


SCHEMA = '{"type":"object","properties":{"verdict":{"enum":["good","bad"]},"defects":{"type":"array","items":{"type":"string"}}},"required":["verdict","defects"]}'

_SCHEMA_MODE_TEXT = json.dumps(
    {
        "headline": "h",
        "summary": "s",
        "key_actions": [],
        "evidence": "e",
        "hypotheses": [],
        "result_focus_findings": {"verdict": "bad", "defects": ["x"]},
        "attempts": [],
    }
)

_PROSE_MODE_TEXT = (
    '{"headline":"agent enumerated cheats",'
    '"summary":"...",'
    '"key_actions":["read tests"],'
    '"evidence":"transcript step 4",'
    '"result_focus_findings":"found two ambiguities",'
    '"attempts":[{"title":"hardcode results.json","rationale":"verifier reads it","outcome":"rejected","success":false,"step_indices":[1,2,3]}]'
    "}"
)


@pytest.mark.asyncio
async def test_schema_mode_returns_object_findings_and_enforces(monkeypatch):
    calls = _patch_acompletion(monkeypatch, _SCHEMA_MODE_TEXT)
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
    fmt = calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"]["result_focus_findings"]["properties"][
        "verdict"
    ] == {"enum": ["good", "bad"]}


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


def test_normalize_keeps_result_focus_summary():
    out = _normalize_probe_summary(
        {
            "result_focus_summary": "  Verdict: solvable only by guessing; 3 blockers.  ",
            "result_focus_findings": {"verdict": "guess"},
        },
        result_focus="{}",
        model="m",
    )
    assert (
        out["result_focus_summary"] == "Verdict: solvable only by guessing; 3 blockers."
    )


def test_normalize_result_focus_summary_absent_is_none():
    out = _normalize_probe_summary(
        {"result_focus_findings": "x"}, result_focus="q?", model="m"
    )
    assert out["result_focus_summary"] is None


def test_build_envelope_includes_result_focus_summary():
    env = _build_envelope_schema(None)
    assert env["properties"]["result_focus_summary"]["type"] == ["string", "null"]
    assert "result_focus_summary" in env["required"]


def test_build_envelope_nests_findings_schema():
    env = _build_envelope_schema({"type": "object", "properties": {}})
    assert env["additionalProperties"] is False
    assert env["properties"]["result_focus_findings"]["type"] == "object"


@pytest.mark.asyncio
async def test_probe_analyzer_parses_json_response(monkeypatch):
    """Happy path: model returns clean JSON, analyzer fills the dict."""
    calls = _patch_acompletion(monkeypatch, _PROSE_MODE_TEXT)

    result = await run_probe_analyzer(
        extra_instructions="find cheats",
        agent_messages=[{"kind": "assistant_text", "text": "Looking at tests..."}],
        verifier_stdout="passed: 0",
        reward=0.0,
        result_focus="Did the agent find spec ambiguities?",
    )

    assert result["kind"] == "probe_summary"
    assert result["headline"] == "agent enumerated cheats"
    assert "cheating_attempted" not in result
    assert "cheating_succeeded" not in result
    assert "recommendations" not in result
    assert result["model"] == "claude-sonnet-4-6"
    assert isinstance(result["attempts"], list)
    assert result["attempts"][0]["title"] == "hardcode results.json"
    assert result["attempts"][0]["step_indices"] == [1, 2, 3]
    assert result["attempts"][0]["success"] is False
    assert result["result_focus_findings"] == "found two ambiguities"
    assert result["result_focus_question"] == "Did the agent find spec ambiguities?"
    # prose-mode result_focus must NOT trigger structured output enforcement
    assert "output_config" not in calls[0]


@pytest.mark.asyncio
async def test_probe_analyzer_routes_bedrock_model_through_direct_api(monkeypatch):
    """A cloud Bedrock model id is normalized to the plain id and run on the
    direct Anthropic API (``anthropic/`` litellm route) -- never Bedrock."""
    calls = _patch_acompletion(monkeypatch, _PROSE_MODE_TEXT)

    result = await run_probe_analyzer(
        extra_instructions="",
        agent_messages=[{"kind": "assistant_text", "text": "..."}],
        verifier_stdout="",
        reward=1.0,
        result_focus="",
        model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
    )

    # The Bedrock inference-profile id is resolved to its plain API id, both for
    # the actual API call and for the model recorded on the summary.
    assert result["model"] == "claude-haiku-4-5"
    assert calls[0]["model"] == "anthropic/claude-haiku-4-5"


# ---------------------------------------------------------------------------
# _normalize_probe_summary — cheating / recommendations dropped
# ---------------------------------------------------------------------------


def _normalize(parsed: dict) -> dict:
    return _normalize_probe_summary(parsed, result_focus="", model="m")


def test_normalize_omits_cheating_and_recommendations():
    """The probe is adversarial by design, so the analyzer no longer emits a
    cheat verdict or operator recommendations — both are dropped even when the
    model returns them."""
    out = _normalize(
        {
            "headline": "hi",
            "cheating_attempted": True,
            "cheating_succeeded": True,
            "recommendations": [
                {"priority": "must_fix", "action": "Compute results from source"}
            ],
        }
    )
    assert "cheating_attempted" not in out
    assert "cheating_succeeded" not in out
    assert "recommendations" not in out


def test_build_envelope_omits_cheating_and_recommendations():
    env = _build_envelope_schema(None)
    assert "cheating_attempted" not in env["properties"]
    assert "cheating_succeeded" not in env["properties"]
    assert "recommendations" not in env["properties"]


# ---------------------------------------------------------------------------
# _coerce_analyzer_json — tolerate a response truncated by the token cap
# ---------------------------------------------------------------------------


def test_coerce_json_passes_through_valid():
    assert _coerce_analyzer_json('{"headline": "ok", "attempts": []}') == {
        "headline": "ok",
        "attempts": [],
    }


def test_coerce_json_repairs_unterminated_string():
    """The reported failure: the model ran out of tokens mid-string. We keep the
    last complete value rather than dropping the whole summary."""
    truncated = '{"headline": "agent audited the task", "summary": "it began to expla'
    out = _coerce_analyzer_json(truncated)
    assert out["headline"] == "agent audited the task"
    assert "summary" not in out  # the incomplete value is dropped, not invented


def test_coerce_json_repairs_truncated_attempts_array():
    """A cut-off inside a nested array keeps the attempts that fully arrived."""
    truncated = (
        '{"headline": "h", "attempts": ['
        '{"title": "first", "success": false}, '
        '{"title": "seco'
    )
    out = _coerce_analyzer_json(truncated)
    assert out["headline"] == "h"
    assert [a["title"] for a in out["attempts"]] == ["first"]


def test_coerce_json_unsalvageable_raises():
    import json

    with pytest.raises(json.JSONDecodeError):
        _coerce_analyzer_json('{"headline": "no complete value yet')


@pytest.mark.asyncio
async def test_probe_analyzer_recovers_from_truncated_response(monkeypatch):
    """End to end: a max_tokens-truncated model response yields a summary instead
    of bubbling a JSONDecodeError up as 'Summary failed'."""
    _patch_acompletion(
        monkeypatch,
        '{"headline": "agent enumerated cheats", '
        '"summary": "the agent read the verifier and then started to wri',
    )

    result = await run_probe_analyzer(
        extra_instructions="find cheats",
        agent_messages=[{"kind": "assistant_text", "text": "..."}],
        verifier_stdout="",
        reward=0.0,
        result_focus="",
    )

    assert result["kind"] == "probe_summary"
    assert result["headline"] == "agent enumerated cheats"


@pytest.mark.asyncio
async def test_probe_analyzer_prompt_frames_subject_as_probe_and_drops_cheat_recs(
    monkeypatch,
):
    """The prompt must tell the analyzer its subject is a privileged PROBE (so it
    does not misread sanctioned probe actions as task holes) and must no longer
    ask for a cheat verdict or operator recommendations."""
    calls = _patch_acompletion(monkeypatch, _PROSE_MODE_TEXT)

    await run_probe_analyzer(
        extra_instructions="find cheats",
        agent_messages=[{"kind": "assistant_text", "text": "..."}],
        verifier_stdout="",
        reward=0.0,
        result_focus="",
    )

    sent = calls[0]["messages"][0]["content"]
    assert "PROBE" in sent
    assert "/probe-harness/app-baseline" in sent
    assert '"recommendations"' not in sent
    assert '"cheating_attempted"' not in sent
