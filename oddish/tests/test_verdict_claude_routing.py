"""Verdict synthesis defaults to the shared analysis model (Claude).

The dedicated gpt-5.4 ``VERDICT_MODEL`` is deprecated: ``settings.verdict_model``
now defaults to ``ANALYSIS_MODEL``, Claude ids route the verdict call through
the Claude CLI (same auth/routing as the trial classifier), and a non-Claude
``ODDISH_VERDICT_MODEL`` override keeps the OpenAI / Azure OpenAI client path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.analyze import classifier  # noqa: E402
from oddish.analyze.models import (  # noqa: E402
    Classification,
    TrialClassification,
)
from oddish.config import ANALYSIS_MODEL, settings  # noqa: E402


def _classification() -> TrialClassification:
    return TrialClassification(
        trial_name="trial-1",
        classification=Classification.GOOD_SUCCESS,
        subtype="Clean Success",
        evidence="agent solved the task",
        root_cause="",
        recommendation="",
        reward=1.0,
    )


def test_verdict_model_defaults_to_analysis_model() -> None:
    assert settings.verdict_model == settings.analysis_model == ANALYSIS_MODEL
    assert settings.get_qa_queue_key() == settings.get_analysis_queue_key()


def test_is_claude_model_id_covers_all_claude_spellings() -> None:
    assert classifier._is_claude_model_id("claude-sonnet-5")
    assert classifier._is_claude_model_id("anthropic/claude-sonnet-5")
    assert classifier._is_claude_model_id("global.anthropic.claude-sonnet-4-6")
    assert not classifier._is_claude_model_id("gpt-5.4")
    assert not classifier._is_claude_model_id("openai/gpt-5.4")


def test_default_model_routes_to_claude_cli(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def _fake_claude(classifications, baseline, quality_check_passed, model, *a, **kw):
        calls["model"] = model
        return "claude-verdict"

    def _fail_openai(*a, **kw):
        raise AssertionError("OpenAI path must not run for Claude models")

    monkeypatch.setattr(classifier, "_compute_task_verdict_claude", _fake_claude)
    monkeypatch.setattr(classifier, "_compute_task_verdict_openai", _fail_openai)

    result = classifier.compute_task_verdict([_classification()])

    assert result == "claude-verdict"
    assert calls["model"] == ANALYSIS_MODEL


def test_non_claude_override_keeps_openai_path(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def _fake_openai(classifications, baseline, quality_check_passed, model, *a, **kw):
        calls["model"] = model
        return "openai-verdict"

    def _fail_claude(*a, **kw):
        raise AssertionError("Claude path must not run for non-Claude models")

    monkeypatch.setattr(classifier, "_compute_task_verdict_openai", _fake_openai)
    monkeypatch.setattr(classifier, "_compute_task_verdict_claude", _fail_claude)

    result = classifier.compute_task_verdict([_classification()], model="gpt-5.4")

    assert result == "openai-verdict"
    assert calls["model"] == "gpt-5.4"


def test_empty_classifications_short_circuit_before_any_client(monkeypatch) -> None:
    def _fail(*a, **kw):
        raise AssertionError("no model call expected for empty classifications")

    monkeypatch.setattr(classifier, "_compute_task_verdict_claude", _fail)
    monkeypatch.setattr(classifier, "_compute_task_verdict_openai", _fail)

    verdict = classifier.compute_task_verdict([])

    assert verdict.is_good is False
    assert verdict.primary_issue == "No trials to analyze"


def test_claude_cli_verdict_parses_structured_output(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "structured_output": {
                    "is_good": True,
                    "confidence": "high",
                    "primary_issue": None,
                    "reasoning": "all trials clean",
                    "recommendations": [],
                }
            }
        ).encode()
        stderr = b""

    def _fake_run(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs.get("timeout")
        return _Completed()

    monkeypatch.setattr(classifier.subprocess, "run", _fake_run)

    verdict = classifier.compute_task_verdict(
        [_classification()], model="claude-sonnet-5", timeout=42
    )

    assert verdict.is_good is True
    assert verdict.confidence == "high"
    assert verdict.success_count == 1
    assert verdict.task_problem_count == 0
    assert seen["timeout"] == 42
    command = seen["command"]
    assert "--json-schema" in command
    # Plain Claude id resolves through the analysis-model routing (direct
    # Anthropic API id, not a Bedrock profile).
    model_flag_value = command[command.index("--model") + 1]
    assert model_flag_value == "claude-sonnet-5"


def test_claude_cli_failure_surfaces_as_verdict_error(monkeypatch) -> None:
    class _Completed:
        returncode = 1
        stdout = b""
        stderr = b"boom"

    monkeypatch.setattr(classifier.subprocess, "run", lambda *a, **kw: _Completed())

    with pytest.raises(RuntimeError, match="Verdict synthesis failed"):
        classifier.compute_task_verdict(
            [_classification()], model="claude-sonnet-5", timeout=5
        )
