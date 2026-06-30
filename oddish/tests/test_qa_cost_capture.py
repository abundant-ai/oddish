"""Unit tests for QA-cost extraction helpers.

These price the LLM calls of the QA / analysis pipeline so the spend can be
reported alongside trial-execution spend:

* ``_extract_cli_cost_usd`` -- the trajectory classifier's Claude Code CLI
  call (native ``total_cost_usd`` preferred, token estimate as fallback);
* ``_extract_openai_cost_usd`` -- the per-task verdict synthesis OpenAI call;
* ``_estimate_message_cost_usd`` -- the probe-summary Anthropic Messages call.

Pure functions, no network or DB.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.analyze.classifier import (  # noqa: E402
    _extract_cli_cost_usd,
    _extract_openai_cost_usd,
)
from oddish.model_pricing import estimate_cost_usd  # noqa: E402
from oddish.worker.probe_analysis import _estimate_message_cost_usd  # noqa: E402

# Models the pricing table can resolve, so the estimates are deterministic.
_OPENAI_MODEL = "gpt-5.5-pro"
_ANTHROPIC_MODEL = "claude-opus-4-8"


def test_cli_cost_prefers_native_total_cost_usd():
    payload = {
        "total_cost_usd": 0.1234,
        # usage present but must be ignored when native cost is reported.
        "usage": {"input_tokens": 9999, "output_tokens": 9999},
    }
    assert _extract_cli_cost_usd(payload, _ANTHROPIC_MODEL) == 0.1234


def test_cli_cost_falls_back_to_token_estimate():
    usage = {
        "input_tokens": 10_000,
        "output_tokens": 4_000,
        "cache_creation_input_tokens": 1_000,
        "cache_read_input_tokens": 2_000,
    }
    payload = {"usage": usage}  # no total_cost_usd
    expected = estimate_cost_usd(
        _ANTHROPIC_MODEL,
        10_000 + 1_000 + 2_000,
        4_000,
        2_000,
    )
    assert expected is not None and expected > 0
    got = _extract_cli_cost_usd(payload, _ANTHROPIC_MODEL)
    assert got is not None and abs(got - expected) < 1e-9


def test_cli_cost_none_when_no_cost_and_no_usage():
    assert _extract_cli_cost_usd({}, _ANTHROPIC_MODEL) is None
    assert _extract_cli_cost_usd({"result": "x"}, _ANTHROPIC_MODEL) is None


def test_openai_cost_from_usage_with_cached_tokens():
    usage = SimpleNamespace(
        prompt_tokens=8_000,
        completion_tokens=2_000,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3_000),
    )
    expected = estimate_cost_usd(_OPENAI_MODEL, 8_000, 2_000, 3_000)
    assert expected is not None and expected > 0
    got = _extract_openai_cost_usd(usage, _OPENAI_MODEL)
    assert got is not None and abs(got - expected) < 1e-9


def test_openai_cost_none_when_usage_missing():
    assert _extract_openai_cost_usd(None, _OPENAI_MODEL) is None


def test_probe_message_cost_from_anthropic_usage():
    usage = SimpleNamespace(
        input_tokens=5_000,
        output_tokens=1_500,
        cache_creation_input_tokens=500,
        cache_read_input_tokens=1_000,
    )
    expected = estimate_cost_usd(
        _ANTHROPIC_MODEL,
        5_000 + 500 + 1_000,
        1_500,
        1_000,
    )
    assert expected is not None and expected > 0
    got = _estimate_message_cost_usd(usage, _ANTHROPIC_MODEL)
    assert got is not None and abs(got - expected) < 1e-9


def test_probe_message_cost_none_when_usage_missing():
    assert _estimate_message_cost_usd(None, _ANTHROPIC_MODEL) is None
