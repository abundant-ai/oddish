from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import NOP_ORACLE_QUEUE_KEY, Settings  # noqa: E402


def _settings(monkeypatch, **kwargs) -> Settings:
    monkeypatch.delenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES", raising=False)
    monkeypatch.delenv("ODDISH_NOP_ORACLE_CONCURRENCY", raising=False)
    return Settings(_env_file=None, **kwargs)


def test_nop_and_oracle_use_dedicated_queue_key(monkeypatch):
    settings = _settings(monkeypatch)

    assert settings.normalize_trial_model("nop", None) == "default"
    assert settings.normalize_trial_model("oracle", None) == "default"
    assert settings.get_queue_key_for_trial("nop", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.get_queue_key_for_trial("oracle", None) == NOP_ORACLE_QUEUE_KEY


def test_nop_oracle_queue_has_separate_default_concurrency(monkeypatch):
    settings = _settings(
        monkeypatch,
        default_model_concurrency=8,
        nop_oracle_concurrency=48,
    )

    assert settings.get_model_concurrency("default") == 8
    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 48
    assert NOP_ORACLE_QUEUE_KEY in settings.get_known_queue_keys()


def test_model_concurrency_overrides_can_override_nop_oracle_queue(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        f'{{"{NOP_ORACLE_QUEUE_KEY}": 12, "default": 3}}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 12
    assert settings.get_model_concurrency("default") == 3


def test_claude_trial_model_is_persisted_as_bedrock_id(monkeypatch):
    settings = _settings(monkeypatch)

    expected = "global.anthropic.claude-sonnet-4-6"

    assert (
        settings.normalize_trial_model("claude-code", "claude-sonnet-4-6") == expected
    )
    assert (
        settings.normalize_trial_model("claude-code", "anthropic/claude-sonnet-4-6")
        == expected
    )
    assert (
        settings.get_provider_for_trial("claude-code", "claude-sonnet-4-6") == "bedrock"
    )
    assert (
        settings.get_queue_key_for_trial("claude-code", "claude-sonnet-4-6") == expected
    )
    assert settings.get_provider_for_trial("claude-code", None) == "bedrock"


def test_opus_4_8_maps_to_global_inference_profile(monkeypatch):
    settings = _settings(monkeypatch)

    # Opus 4.8's invokable Bedrock id is the "global." cross-region inference
    # profile (the bare "anthropic.claude-opus-4-8" foundation-model id is not
    # invokable on-demand via the legacy InvokeModel API Claude Code uses).
    expected = "global.anthropic.claude-opus-4-8"

    assert settings.normalize_trial_model("claude-code", "claude-opus-4-8") == expected
    assert (
        settings.normalize_trial_model("claude-code", "anthropic/claude-opus-4-8")
        == expected
    )
    assert settings.normalize_queue_key("claude-opus-4-8") == expected
    assert settings.normalize_queue_key("anthropic/claude-opus-4-8") == expected


def test_bedrock_queue_key_normalization_collapses_aliases(monkeypatch):
    settings = _settings(monkeypatch)

    expected = "global.anthropic.claude-sonnet-4-6"

    assert settings.normalize_queue_key("claude-sonnet-4-6") == expected
    assert settings.normalize_queue_key("anthropic/claude-sonnet-4-6") == expected
    assert settings.normalize_queue_key(f"bedrock/{expected}") == expected


def test_openrouter_claude_model_routes_through_openrouter(monkeypatch):
    settings = _settings(monkeypatch)

    # An explicit openrouter/ prefix must pin the trial to OpenRouter instead
    # of being rewritten to a Bedrock inference-profile id.
    model = "openrouter/anthropic/claude-opus-4.8"

    assert settings.normalize_trial_model("claude-code", model) == model
    assert settings.get_provider_for_trial("claude-code", model) == "openrouter"
    assert settings.get_queue_key_for_trial("claude-code", model) == model
    assert settings.normalize_queue_key(model) == model


def test_legacy_unmapped_claude_queue_key_does_not_break_reads(monkeypatch):
    settings = _settings(monkeypatch)
    legacy_key = "anthropic/claude-sonnet-4-6-20250514"

    assert settings.normalize_queue_key(legacy_key) == legacy_key
    with pytest.raises(ValueError):
        settings.normalize_trial_model("claude-code", legacy_key)
