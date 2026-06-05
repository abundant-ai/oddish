from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import NOP_ORACLE_QUEUE_KEY, Settings  # noqa: E402


def _settings(monkeypatch, *, clear_openai_env: bool = True, **kwargs) -> Settings:
    monkeypatch.delenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES", raising=False)
    monkeypatch.delenv("ODDISH_NOP_ORACLE_CONCURRENCY", raising=False)
    if clear_openai_env:
        monkeypatch.delenv("ODDISH_OPENAI_PROVIDER", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
        monkeypatch.delenv("ODDISH_AZURE_OPENAI_DEPLOYMENTS", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
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
    settings = _settings(monkeypatch, clear_openai_env=False)

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
    settings = _settings(monkeypatch, clear_openai_env=False)

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
    settings = _settings(monkeypatch, clear_openai_env=False)

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
    settings = _settings(monkeypatch, clear_openai_env=False)
    legacy_key = "anthropic/claude-sonnet-4-6-20250514"

    assert settings.normalize_queue_key(legacy_key) == legacy_key
    with pytest.raises(ValueError):
        settings.normalize_trial_model("claude-code", legacy_key)


def test_openai_provider_defaults_to_azure(monkeypatch):
    settings = _settings(monkeypatch)

    assert settings.get_openai_provider() == "azure"
    with pytest.raises(RuntimeError, match="Azure OpenAI is the default"):
        settings.get_openai_runtime_env(model="openai/gpt-5.2")


def test_openai_api_key_alone_does_not_enable_public_openai(monkeypatch):
    settings = _settings(monkeypatch, openai_api_key="sk-test")

    assert settings.get_openai_provider() == "azure"
    with pytest.raises(RuntimeError, match="Azure OpenAI is the default"):
        settings.get_openai_runtime_env(model="openai/gpt-5.2")


def test_azure_openai_deployment_mapping_accepts_prefixed_and_bare_models(
    monkeypatch,
):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={
            "openai/gpt-5.2": "azure-gpt-5-2",
            "gpt-5.4": "azure-gpt-5-4",
        },
    )

    assert settings.resolve_azure_openai_deployment("openai/gpt-5.2") == (
        "azure-gpt-5-2"
    )
    assert settings.resolve_azure_openai_deployment("gpt-5.2") == "azure-gpt-5-2"
    assert settings.resolve_azure_openai_deployment("openai/gpt-5.4") == (
        "azure-gpt-5-4"
    )


def test_azure_openai_deployment_mapping_normalizes_model_keys(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{" OpenAI / GPT 5.2 ":"azure-gpt-5-2"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert settings.azure_openai_deployments == {"openai/gpt-5.2": "azure-gpt-5-2"}
    assert settings.resolve_azure_openai_deployment("openai/gpt-5.2") == (
        "azure-gpt-5-2"
    )


def test_missing_azure_openai_deployment_mapping_fails_loudly(monkeypatch):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={"openai/gpt-5.2": "azure-gpt-5-2"},
    )

    with pytest.raises(ValueError, match="No Azure OpenAI deployment mapping"):
        settings.resolve_azure_openai_deployment("openai/gpt-5.4")


def test_openai_queue_key_preserves_requested_model_name(monkeypatch):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={"openai/gpt-5.2": "azure-gpt-5-2"},
    )

    assert settings.normalize_trial_model("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )
    assert settings.get_queue_key_for_trial("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )


def test_azure_openai_runtime_env_excludes_public_openai_key(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_openai_runtime_env(model="openai/gpt-5.2")

    assert env["AZURE_OPENAI_API_KEY"] == "az-key"
    assert env["AZURE_OPENAI_ENDPOINT"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_OPENAI_API_VERSION"] == "2025-01-01-preview"
    assert env["AZURE_OPENAI_DEPLOYMENT"] == "oddish-gpt"
    assert env["OPENAI_API_VERSION"] == "2025-01-01-preview"
    assert "OPENAI_API_KEY" not in env


def test_azure_openai_agent_env_uses_compatible_base_url(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_openai_agent_env(model="openai/gpt-5.2")

    assert env["OPENAI_API_KEY"] == "az-key"
    assert env["OPENAI_BASE_URL"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_API_KEY"] == "az-key"
    assert env["AZURE_API_BASE"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_API_VERSION"] == "2025-01-01-preview"


def test_foundry_openai_v1_endpoint_is_allowed(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.services.ai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"gpt-5.2"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert (
        settings.get_azure_openai_base_url()
        == "https://example.services.ai.azure.com/openai/v1"
    )


def test_azure_openai_agent_env_rejects_foundry_project_endpoint(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.services.ai.azure.com/api/projects/oddish",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    with pytest.raises(RuntimeError, match="Do not use the Foundry project endpoint"):
        settings.get_openai_agent_env(model="openai/gpt-5.2")


def test_public_openai_requires_explicit_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = _settings(
        monkeypatch,
        clear_openai_env=False,
        openai_provider="openai",
    )

    assert settings.get_openai_provider() == "openai"
    assert settings.get_openai_runtime_env() == {"OPENAI_API_KEY": "sk-test"}
