from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.blocks.analyzer import analyzer_llm_client as llm  # noqa: E402


def test_verdict_client_defaults_to_mapped_azure_deployment(monkeypatch):
    seen: dict[str, object] = {}

    class _AsyncOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(llm, "AsyncOpenAI", _AsyncOpenAI)
    monkeypatch.setattr(llm.settings, "openai_provider", "azure")
    monkeypatch.setattr(llm.settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        llm.settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setattr(
        llm.settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        llm.settings,
        "azure_openai_deployments",
        {"gpt-5.4": "oddish-gpt"},
    )

    _client, runtime_model = llm._build_openai_client(model="gpt-5.4")

    assert runtime_model == "oddish-gpt"
    assert seen == {
        "api_key": "az-key",
        "base_url": "https://example.openai.azure.com/openai/v1",
    }


def test_verdict_client_fails_when_azure_mapping_is_missing(monkeypatch):
    monkeypatch.setattr(llm.settings, "openai_provider", "azure")
    monkeypatch.setattr(llm.settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        llm.settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setattr(
        llm.settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        llm.settings,
        "azure_openai_deployments",
        {"gpt-5.2": "oddish-gpt"},
    )

    with pytest.raises(ValueError, match="No Azure OpenAI deployment mapping"):
        llm._build_openai_client(model="gpt-5.4")


def test_verdict_client_public_openai_requires_explicit_provider(monkeypatch):
    seen: dict[str, object] = {}

    class _AsyncOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(llm, "AsyncOpenAI", _AsyncOpenAI)
    monkeypatch.setattr(llm.settings, "openai_provider", "openai")
    monkeypatch.setattr(llm.settings, "openai_api_key", "sk-test")

    with pytest.warns(UserWarning, match="public OpenAI API"):
        _client, runtime_model = llm._build_openai_client(model="gpt-5.4")

    assert runtime_model == "gpt-5.4"
    assert seen == {"api_key": "sk-test"}


_AZURE_ENDPOINT = "https://abundant-oddish-foundry.openai.azure.com/openai/v1"
_AZURE_HOST = "abundant-oddish-foundry.openai.azure.com"


def test_azure_compat_codex_allowlists_azure_endpoint(monkeypatch):
    """The Azure Codex variant must declare the Azure host so Harbor's Modal
    egress firewall doesn't blackhole every gpt-5.5 request."""
    from oddish.config import settings as oddish_settings
    from oddish.workers.agents.codex import AzureCompatibleCodex

    monkeypatch.setattr(oddish_settings, "azure_openai_endpoint", _AZURE_ENDPOINT)

    domains = AzureCompatibleCodex.required_outbound_domains(
        model_name="openai/gpt-5.5", kwargs={}
    )

    assert _AZURE_HOST in domains


def test_outbound_hosts_for_model_includes_azure_when_configured(monkeypatch):
    """Oddish host injection must allowlist the Azure OpenAI endpoint for
    openai/* models when the Azure provider is configured."""
    from oddish.config import OPENAI_PROVIDER_AZURE
    from oddish.config import settings as oddish_settings
    from oddish.workers.harbor.model_hosts import outbound_hosts_for_model

    monkeypatch.setattr(oddish_settings, "azure_openai_endpoint", _AZURE_ENDPOINT)
    monkeypatch.setattr(oddish_settings, "openai_provider", OPENAI_PROVIDER_AZURE)

    hosts = outbound_hosts_for_model("openai/gpt-5.5")

    assert _AZURE_HOST in hosts


def test_azure_compat_codex_allowlists_per_trial_openai_base_url():
    """If a trial pins an explicit OPENAI_BASE_URL (extra_env), the hook
    allowlists that host too (mirrors harbor's base Codex hook)."""
    from oddish.workers.agents.codex import AzureCompatibleCodex

    domains = AzureCompatibleCodex.required_outbound_domains(
        model_name="openai/gpt-5.5",
        kwargs={
            "extra_env": {
                "OPENAI_BASE_URL": "https://other-foundry.openai.azure.com/openai/v1"
            }
        },
    )

    assert "other-foundry.openai.azure.com" in domains
